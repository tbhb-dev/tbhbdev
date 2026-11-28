"""`tbhbdev alpha`: interim agent tools, ported from an earlier interim toolset.

Alpha commands are interim. They may change or go away without notice, and
only the tbhb-dev harness hooks and agents in tbhb-dev repositories should
call them.

The harness and Git hooks run on every tool call and commit, so every command
module is imported lazily only when its subcommand is selected.
"""

from __future__ import annotations

import os
import sys
lazy from collections.abc import MutableMapping

from tbhbdev.command_table import Command, CommandTable
lazy from tbhbdev.alpha import (
    brief,
    close,
    gh_app,
    gh_shim,
    pre_push_guard,
    repo_bootstrap,
    review,
    run_stop_hook,
    session_context,
    verify_range,
    version_check,
    worktree_sweep,
)
lazy from tbhbdev.alpha.agent import command as agent_command
lazy from tbhbdev.alpha.guard import (
    agent_model as guard_agent_model,
)
lazy from tbhbdev.alpha.guard import (
    gh_config_dir as guard_gh_config_dir,
)
lazy from tbhbdev.alpha.guard import (
    ops_no_lazy_stop as guard_ops_no_lazy_stop,
)
lazy from tbhbdev.alpha.guard import (
    sendmessage_clock as guard_sendmessage_clock,
)
lazy from tbhbdev.alpha.guard import (
    state as guard_state,
)
lazy from tbhbdev.alpha.guard.gh_poll import command as guard_gh_poll
lazy from tbhbdev.alpha.guard.gh_pr import command as guard_gh_pr
lazy from tbhbdev.alpha.guard.op import command as guard_op
lazy from tbhbdev.alpha.guard.scripts import command as guard_scripts
lazy from tbhbdev.alpha.identity import dispatcher as identity_dispatcher
lazy from tbhbdev.alpha.install import command as install_command_module
lazy from tbhbdev.alpha.merge import command as merge_command_module
lazy from tbhbdev.alpha.monorepo import command as monorepo_command_module
lazy from tbhbdev.alpha.pull_request import command as pull_request_command

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


def identity_command(argv: list[str], _prog: str) -> int:
    return identity_dispatcher.run(argv, os.environ)


def guard_command(_argv: list[str], _prog: str) -> int:
    return guard_gh_pr.main()


def guard_op_command(_argv: list[str], _prog: str) -> int:
    return guard_op.main()


def state_guard_command(_argv: list[str], _prog: str) -> int:
    return guard_state.main()


def session_context_command(_argv: list[str], _prog: str) -> int:
    return session_context.main()


def poll_command(_argv: list[str], _prog: str) -> int:
    return guard_gh_poll.main()


def sendmessage_clock_command(_argv: list[str], _prog: str) -> int:
    return guard_sendmessage_clock.main()


def ops_no_lazy_stop_command(_argv: list[str], _prog: str) -> int:
    return guard_ops_no_lazy_stop.main()


def gh_config_dir_command(_argv: list[str], _prog: str) -> int:
    return guard_gh_config_dir.main()


def agent_model_command(_argv: list[str], _prog: str) -> int:
    return guard_agent_model.main()


def scripts_command(_argv: list[str], _prog: str) -> int:
    return guard_scripts.main()


def pr(argv: list[str], prog: str) -> int:
    # Every close goes through close.py's checks: agent session, open PR, ledger.
    if argv[:1] == ["close"]:
        return close.main(["pr", *argv], prog=prog)
    return pull_request_command.main(argv, prog=prog)


def review_command(argv: list[str], prog: str) -> int:
    return review.main(argv, prog=prog)


def issue(argv: list[str], prog: str) -> int:
    return close.main(["issue", *argv], prog=prog)


def gh_app_command(argv: list[str], prog: str) -> int:
    return gh_app.main(argv, prog)


def repo_command(argv: list[str], prog: str) -> int:
    return repo_bootstrap.main(argv, prog=prog)


def merge(argv: list[str], prog: str) -> int:
    return merge_command_module.main(argv, prog=prog)


def monorepo_command(argv: list[str], prog: str) -> int:
    return monorepo_command_module.main(argv, prog)


COMMANDS: MutableMapping[str, Command] = CommandTable(
    {
        "identity": lambda: identity_command,
        "guard-gh-pr": lambda: guard_command,
        "guard-op": lambda: guard_op_command,
        "state-guard": lambda: state_guard_command,
        "session-context": lambda: session_context_command,
        "worktree-sweep": lambda: worktree_sweep.main,
        "guard-gh-poll": lambda: poll_command,
        "guard-gh-config-dir": lambda: gh_config_dir_command,
        "guard-sendmessage-clock": lambda: sendmessage_clock_command,
        "guard-ops-no-lazy-stop": lambda: ops_no_lazy_stop_command,
        "guard-scripts": lambda: scripts_command,
        "guard-agent-model": lambda: agent_model_command,
        "gh-shim": lambda: gh_shim.main,
        "agent-env": lambda: gh_shim.agent_env,
        "pr": lambda: pr,
        "review": lambda: review_command,
        "issue": lambda: issue,
        "gh-app": lambda: gh_app_command,
        "repo": lambda: repo_command,
        "merge": lambda: merge,
        "monorepo": lambda: monorepo_command,
        "verify-range": lambda: verify_range.main,
        "version-check": lambda: version_check.main,
        "install": lambda: install_command_module.main,
        "pre-push-guard": lambda: pre_push_guard.main,
        "agent": lambda: agent_command.main,
        "run-stop-hook": lambda: run_stop_hook.main,
        "brief": lambda: brief.main,
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
