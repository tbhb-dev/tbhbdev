"""`tbhbdev alpha`: interim agent tools, ported from an earlier interim toolset.

Alpha commands are interim. They may change or go away without notice, and
only the tbhb-dev harness hooks and agents in tbhb-dev repositories should
call them.
"""

import importlib
import os
import sys
from functools import partial
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["main"]

USAGE = """usage: {prog} <command> [args...]

Alpha commands are interim: they may change or go away without notice.

commands:
  identity     agent identity: record <harness> | trailer MSGFILE [SOURCE]
               | consume | check MSGFILE | stamp-docs
  guard-gh-pr  PreToolUse hook: block gh pr create|edit|merge|ready|close,
               gh issue close, gh run rerun, gh api branch deletes and
               pull request approvals through gh
  guard-op     PreToolUse hook: require the configured 1Password account
  guard-sendmessage-clock PreToolUse hook: block stale or future SendMessage stamps
  guard-ops-no-lazy-stop Stop hook: ask the assistant to verify marked claims
  state-guard  Claude Code hook: block sign-off and co-author trailers
  session-context
               SessionStart hook: name the host, the other hosts and the time
  worktree-sweep classify and clean up merged worktrees
  guard-gh-poll PreToolUse hook: block bulk GitHub reads and token unsets
  guard-gh-config-dir PreToolUse hook: require nonempty GH_CONFIG_DIR for gh
  guard-scripts PreToolUse hook: keep scripts for the operator in
               ~/.local/libexec/tbhb
  guard-agent-model PreToolUse hook: require a model on Agent launches, and
               a fork only on a session model that isn't costly
  gh-shim      budget gh calls made by agent sessions and their scripts
  agent-env    launch a harness with the gh shim first on PATH
  pr           check | create | edit | hook: the pull request draft
               | rerun N [--stuck]: rerun failed jobs of the head; --stuck reruns
               a run of only this PR with a job in progress over an hour,
               cancelled first and rerun in full
               | delete-branch N: delete a finished PR's head branch on origin
               | close N --reason TEXT: close a pull request
  review       draft N [--force] | check N: the review draft
  issue        close N --reason completed|not-planned: close an issue
               | create --repo OWNER/REPO --type TYPE --title TEXT --body-file FILE
               [--label L ...] [--parent N|OWNER/REPO#N] [--reason TEXT] [--dry-run]
               | link-sub PARENT CHILD | unlink-sub PARENT CHILD
               | block BLOCKED --by BLOCKER | unblock BLOCKED --by BLOCKER
               Relationship commands accept --dry-run and owner/repo#N or #N.
               For dependencies, --blocked-by aliases --by, and
               --blocking takes BLOCKED with BLOCKER as the positional issue.
  gh-app       APP OWNER/REPO [--fresh] [--dry-run] [--all-repos]: mint a GitHub
               App installation token through Connect | APP --cooldown-until
               EPOCH | APP --cooldown-status
  repo         bootstrap [--dry-run] [--create --private|--public] ORG/REPO:
               apply the standard repository settings
  merge        merge an approved pull request
  merge-queue-remove N --reason TEXT [--dry-run]: take an open pull request out
               of its merge queue; it stays open and approved. Re-queue with merge
  monorepo     map CLONE COMMIT --dest FOLDER | import CLONE@COMMIT --map MAP
               --into CLONE | verify CLONE@COMMIT --map MAP --tree CLONE:
               import a public repository into the monorepo
  verify-range BASE TIP [-C PATH]: check every agent commit in a range
  version-check [--host NAME ...] [--expect SHA|latest] [--json]: compare the
               installed build on each host
  agent        exec | wait | status | kill | clean: headless runs
  run-stop-hook <claude|codex|antigravity>: check whether an exec run can stop
  pre-push-guard REMOTE URL: refuse protected-branch and unfetched-rewrite pushes
  brief        new | check: peer-run briefs with required safeguards
  install      install the git hooks and the user-level harness hooks
"""


COMMAND_MODULES = {
    "identity": ("tbhbdev.alpha.identity.dispatcher", "run"),
    "guard-gh-pr": ("tbhbdev.alpha.guard.gh_pr.command", "main"),
    "guard-op": ("tbhbdev.alpha.guard.op.command", "main"),
    "state-guard": ("tbhbdev.alpha.guard.state", "main"),
    "session-context": ("tbhbdev.alpha.session_context", "main"),
    "worktree-sweep": ("tbhbdev.alpha.worktree_sweep", "main"),
    "guard-gh-poll": ("tbhbdev.alpha.guard.gh_poll.command", "main"),
    "guard-gh-config-dir": ("tbhbdev.alpha.guard.gh_config_dir", "main"),
    "guard-sendmessage-clock": ("tbhbdev.alpha.guard.sendmessage_clock", "main"),
    "guard-ops-no-lazy-stop": ("tbhbdev.alpha.guard.ops_no_lazy_stop", "main"),
    "guard-scripts": ("tbhbdev.alpha.guard.scripts.command", "main"),
    "guard-agent-model": ("tbhbdev.alpha.guard.agent_model", "main"),
    "gh-shim": ("tbhbdev.alpha.gh_shim", "main"),
    "agent-env": ("tbhbdev.alpha.gh_shim", "agent_env"),
    "pr": ("tbhbdev.alpha.pull_request.command", "main"),
    "review": ("tbhbdev.alpha.review", "main"),
    "issue": ("tbhbdev.alpha.close", "main"),
    "gh-app": ("tbhbdev.alpha.gh_app_command", "main"),
    "repo": ("tbhbdev.alpha.repo_bootstrap", "main"),
    "merge": ("tbhbdev.merge.command", "main"),
    "merge-queue-remove": ("tbhbdev.merge.remove", "main"),
    "monorepo": ("tbhbdev.alpha.monorepo.command", "main"),
    "verify-range": ("tbhbdev.alpha.verify_range", "main"),
    "version-check": ("tbhbdev.alpha.version_check", "main"),
    "install": ("tbhbdev.alpha.install.command", "main"),
    "pre-push-guard": ("tbhbdev.alpha.pre_push_guard", "main"),
    "agent": ("tbhbdev.alpha.agent.command", "main"),
    "run-stop-hook": ("tbhbdev.alpha.run_stop_hook", "main"),
    "brief": ("tbhbdev.alpha.brief", "main"),
}


def _run(name: str, prog: str, /, *args: object, **kwargs: object) -> int:
    module_name, function_name = COMMAND_MODULES[name]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name
        if missing is None or not (
            module_name == missing or module_name.startswith(f"{missing}.")
        ):
            raise
        sys.stderr.write(f"{prog}: cannot load module {module_name!r}\n")
        return 1
    command = cast("Callable[..., int]", getattr(module, function_name))
    return command(*args, **kwargs)


def _standard(name: str, argv: list[str], prog: str) -> int:
    return _run(name, prog, argv, prog)


def _hook(name: str, _argv: list[str], prog: str) -> int:
    return _run(name, prog)


def _keyword_prog(name: str, argv: list[str], prog: str) -> int:
    return _run(name, prog, argv, prog=prog)


def identity_command(argv: list[str], prog: str) -> int:
    return _run("identity", prog, argv, os.environ)


def pr(argv: list[str], prog: str) -> int:
    # Every close goes through close.py's checks: agent session, open PR, ledger.
    if argv[:1] == ["close"]:
        return _run("issue", prog, ["pr", *argv], prog=prog)
    return _run("pr", prog, argv, prog=prog)


def issue(argv: list[str], prog: str) -> int:
    return _run("issue", prog, ["issue", *argv], prog=prog)


COMMANDS: dict[str, Callable[[list[str], str], int]] = {
    name: partial(_standard, name) for name in COMMAND_MODULES
}
COMMANDS.update(
    {
        "identity": identity_command,
        "guard-gh-pr": partial(_hook, "guard-gh-pr"),
        "guard-op": partial(_hook, "guard-op"),
        "state-guard": partial(_hook, "state-guard"),
        "session-context": partial(_hook, "session-context"),
        "guard-gh-poll": partial(_hook, "guard-gh-poll"),
        "guard-gh-config-dir": partial(_hook, "guard-gh-config-dir"),
        "guard-sendmessage-clock": partial(_hook, "guard-sendmessage-clock"),
        "guard-ops-no-lazy-stop": partial(_hook, "guard-ops-no-lazy-stop"),
        "guard-scripts": partial(_hook, "guard-scripts"),
        "guard-agent-model": partial(_hook, "guard-agent-model"),
        "pr": pr,
        "review": partial(_keyword_prog, "review"),
        "issue": issue,
        "repo": partial(_keyword_prog, "repo"),
        "merge": partial(_keyword_prog, "merge"),
        "merge-queue-remove": partial(_keyword_prog, "merge-queue-remove"),
    }
)


def main(argv: list[str], prog: str) -> int:
    """Run the alpha command argv names, passing it the rest of argv."""
    if not argv or argv[0] in {"-h", "--help"}:
        stream = sys.stdout if argv else sys.stderr
        stream.write(USAGE.format(prog=prog))
        return 0 if argv else 2
    command = COMMANDS.get(argv[0])
    if command is None:
        sys.stderr.write(f"{prog}: unknown command {argv[0]!r}\n")
        sys.stderr.write(USAGE.format(prog=prog))
        return 2
    return command(argv[1:], f"{prog} {argv[0]}")
