"""`tbhbdev alpha`: interim agent tools, ported from an earlier interim toolset.

Alpha commands are interim. They may change or go away without notice, and
only the tbhb-dev harness hooks and agents in tbhb-dev repositories should
call them.

The harness hooks run on every tool call, so this module imports pr, review,
gh_app and merge, which load yaml, jsonschema and the Connect client, only when
one of them runs.
"""

import os
import sys
from typing import TYPE_CHECKING

import tbhbdev.alpha.agent.command
import tbhbdev.alpha.brief
import tbhbdev.alpha.chase_context
import tbhbdev.alpha.gh_shim
import tbhbdev.alpha.guard_agent_model
import tbhbdev.alpha.guard_gh_config_dir
import tbhbdev.alpha.guard_gh_poll
import tbhbdev.alpha.guard_gh_pr
import tbhbdev.alpha.guard_op
import tbhbdev.alpha.guard_ops_no_lazy_stop
import tbhbdev.alpha.guard_scripts
import tbhbdev.alpha.guard_sendmessage_clock
import tbhbdev.alpha.guard_ssh_agent
import tbhbdev.alpha.identity.dispatcher
import tbhbdev.alpha.install.command
import tbhbdev.alpha.pre_push_guard
import tbhbdev.alpha.run_stop_hook
import tbhbdev.alpha.session_context
import tbhbdev.alpha.state_guard
import tbhbdev.alpha.verify_range
import tbhbdev.alpha.version_check
import tbhbdev.alpha.worktree_sweep

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
  guard-ssh-agent PreToolUse hook: block ssh-add and paths in the ssh dir (not sessions)
  guard-sendmessage-clock PreToolUse hook: block stale or future SendMessage stamps
  guard-ops-no-lazy-stop Stop hook: ask the assistant to verify marked claims
  state-guard  Claude Code hook: block sign-off commits and unchecked open items
  session-context
               SessionStart hook: name the host, the other hosts and the time
  chase-context UserPromptSubmit hook: show stale pull requests to coordination
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
               | backfill-opened OWNER: the opened ledgers
               | rerun N [--stuck]: rerun failed jobs of the head; --stuck reruns
               a run of only this PR with a job in progress over an hour,
               cancelled first and rerun in full
               | delete-branch N: delete a finished PR's head branch on origin
               | close N --reason TEXT: close a pull request (--comment is an alias)
  pr-stats     OWNER/REPO [--limit N]: cycle time of merged and closed pull requests
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
  agent        exec | wait | status | kill | clean | keep: headless runs
  run-stop-hook <claude|codex|antigravity>: check whether an exec run can stop
  pre-push-guard REMOTE URL: refuse protected-branch and unfetched-rewrite pushes
  brief        new | check: peer-run briefs with required safeguards
  install      install the git hooks and the user-level harness hooks
"""


def identity_command(argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.identity.dispatcher.run(argv, os.environ)


def guard_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_gh_pr.main()


def guard_op_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_op.main()


def state_guard_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.state_guard.main()


def session_context_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.session_context.main()


def poll_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_gh_poll.main()


def ssh_agent_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_ssh_agent.main()


def sendmessage_clock_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_sendmessage_clock.main()


def ops_no_lazy_stop_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_ops_no_lazy_stop.main()


def gh_config_dir_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_gh_config_dir.main()


def agent_model_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_agent_model.main()


def scripts_command(_argv: list[str], _prog: str) -> int:
    return tbhbdev.alpha.guard_scripts.main()


def pr(argv: list[str], prog: str) -> int:
    # Every close goes through close.py's checks: agent session, open PR, ledger.
    if argv[:1] == ["close"]:
        from tbhbdev.alpha.close import main as close_main  # noqa: PLC0415 -- see the docstring

        return close_main(["pr", *argv], prog=prog)
    from tbhbdev.alpha.pull_request import main as pull_request_main  # noqa: PLC0415 -- see the docstring

    return pull_request_main(argv, prog=prog)


def pr_stats_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha.pr_stats import main as pr_stats_main  # noqa: PLC0415 -- see the docstring

    return pr_stats_main(argv, prog=prog)


def review_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha.review import main as review_main  # noqa: PLC0415 -- see the docstring

    return review_main(argv, prog=prog)


def issue(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha.close import main as close_main  # noqa: PLC0415 -- see the docstring

    return close_main(["issue", *argv], prog=prog)


def gh_app_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha.gh_app import main as gh_app_main  # noqa: PLC0415 -- see the docstring

    return gh_app_main(argv, prog)


def repo_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha.repo_bootstrap import main as repo_main  # noqa: PLC0415 -- see the docstring

    return repo_main(argv, prog=prog)


def merge(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha.merge.command import main as merge_main  # noqa: PLC0415 -- see the docstring

    return merge_main(argv, prog=prog)


def monorepo_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha.monorepo.command import main as monorepo_main  # noqa: PLC0415 -- see the docstring

    return monorepo_main(argv, prog)


def install_command(argv: list[str], prog: str) -> int:
    return tbhbdev.alpha.install.command.main(argv, prog)


COMMANDS: dict[str, Callable[[list[str], str], int]] = {
    "identity": identity_command,
    "guard-gh-pr": guard_command,
    "guard-op": guard_op_command,
    "state-guard": state_guard_command,
    "session-context": session_context_command,
    "chase-context": tbhbdev.alpha.chase_context.main,
    "worktree-sweep": tbhbdev.alpha.worktree_sweep.main,
    "guard-gh-poll": poll_command,
    "guard-gh-config-dir": gh_config_dir_command,
    "guard-ssh-agent": ssh_agent_command,
    "guard-sendmessage-clock": sendmessage_clock_command,
    "guard-ops-no-lazy-stop": ops_no_lazy_stop_command,
    "guard-scripts": scripts_command,
    "guard-agent-model": agent_model_command,
    "gh-shim": tbhbdev.alpha.gh_shim.main,
    "agent-env": tbhbdev.alpha.gh_shim.agent_env,
    "pr": pr,
    "pr-stats": pr_stats_command,
    "review": review_command,
    "issue": issue,
    "gh-app": gh_app_command,
    "repo": repo_command,
    "merge": merge,
    "monorepo": monorepo_command,
    "verify-range": tbhbdev.alpha.verify_range.main,
    "version-check": tbhbdev.alpha.version_check.main,
    "install": install_command,
    "pre-push-guard": tbhbdev.alpha.pre_push_guard.main,
    "agent": tbhbdev.alpha.agent.command.main,
    "run-stop-hook": tbhbdev.alpha.run_stop_hook.main,
    "brief": tbhbdev.alpha.brief.main,
}


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
