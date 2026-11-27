# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse and dispatch the pull request CLI commands."""

import os
import sys
from typing import TYPE_CHECKING, cast

from tbhbdev import userconfig
from tbhbdev.alpha.agent import settings
from tbhbdev.alpha.agent.decisions import host_names
from tbhbdev.alpha.identity import dispatcher as agent_identity
from tbhbdev.alpha.install import require_git_hooks

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping, Sequence
from tbhbdev.alpha.pull_request import attribution as pr_attribution
from tbhbdev.alpha.pull_request import draft_io as pr_draft_io
from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import publish as pr_publish
from tbhbdev.alpha.pull_request import repository as pr_repository
from tbhbdev.alpha.pull_request import sizing as pr_sizing
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.arguments import build_parser
from tbhbdev.alpha.pull_request.attribution import description, live_pr, pr_trailers
from tbhbdev.alpha.pull_request.checks import checks
from tbhbdev.alpha.pull_request.delete_branch import delete_pr_branch
from tbhbdev.alpha.pull_request.hook import hook
from tbhbdev.alpha.pull_request.metadata import gh_args, target_of_pr
from tbhbdev.alpha.pull_request.prose import LINTERS
from tbhbdev.alpha.pull_request.ready import ready
from tbhbdev.alpha.pull_request.rebase import rebase
from tbhbdev.alpha.pull_request.repository import repo_visibility
from tbhbdev.alpha.pull_request.rerun import rerun, rerun_stuck
from tbhbdev.alpha.pull_request.tools import (
    COG,
    GH,
    GIT,
    GITLEAKS,
    SCC,
)


def command(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Resolve the tools for check and create before running the subcommand."""
    if args.command == "create" and not require_git_hooks(env):
        return 1
    if args.command not in ("check", "create"):
        return _run_command(args, env)
    names = (COG, *(name for name, _, _ in LINTERS), GIT, SCC)
    unwrapped = (GITLEAKS,)
    if args.command == "create":
        names += (GITLEAKS, GH)
    with pr_tools.resolved_tools(names, unwrapped=unwrapped):
        return _run_command(args, env)


def _run_command(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Check the draft and carry out the requested action."""
    if args.command == "create":
        code, branch = pr_tools.git("branch", "--show-current")
        if code or not branch:
            print("tbhbdev alpha pr: cannot read the current branch", file=sys.stderr)
            return 1
        if host_names.contains_host_name(branch, settings.known_host_names()):
            print("tbhbdev alpha pr: branch name contains a host name", file=sys.stderr)
            return 1
    checked = pr_draft_io.read_checked(args.draft)
    if args.command in ("check", "create"):
        pr_sizing.check_size(args, checked)
    if checked.problems:
        return 1
    if args.command == "check":
        print("OK: the draft conforms")
        return 0
    if args.command == "create":
        root, line = pr_attribution.opened_by(env)
        body = description(
            checked,
            env,
            pr_attribution.branch_trailers(pr_repository.base_of(checked.meta)),
            line,
        )
        pr_publish.post_text(checked.title, body, pr_repository.base_visibility())
        argv = [GH, "pr", "create", "--title", checked.title, "--body-file", "-"]
        argv += gh_args(checked.meta, pr_repository.base_of(checked.meta))
        return pr_publish.create_pr(argv, body, root, dry_run=args.dry_run)
    line, url = live_pr(args.pr)
    target = target_of_pr(url)
    body = description(checked, env, pr_trailers(args.pr, target), line)
    pr_publish.post_text(checked.title, body, repo_visibility(target))
    argv = [
        GH,
        "pr",
        "edit",
        str(args.pr),
        "--title",
        checked.title,
        "--body-file",
        "-",
    ]
    labels = cast("list[str]", checked.meta.get("labels", []))  # pragma: no mutate
    for label in labels:
        argv += ["--add-label", label]
    return pr_github.invoke_gh(argv, body)[0]


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    prog: str | None = None,
) -> int:
    """Parse the command line and run it."""
    args = build_parser(prog).parse_args(argv)
    env = os.environ if env is None else env
    if args.command == "hook":
        return hook(args.harness)
    if args.command == "rerun":
        return (rerun_stuck if args.stuck else rerun)(args.pr)
    by_pr = {
        "delete-branch": delete_pr_branch,
        "checks": checks,
        "ready": ready,
        "rebase": rebase,
    }
    if args.command in by_pr:
        return by_pr[args.command](args.pr)
    try:
        return command(args, env)
    except userconfig.ConfigError as exc:
        print(f"tbhbdev alpha pr: {exc}", file=sys.stderr)
    except agent_identity.AttributionError:
        print(
            "tbhbdev alpha pr: Attribution couldn't be resolved. Retry the pull "
            "request command from the main session after the subagent's command "
            "finishes and its session record is available.",
            file=sys.stderr,
        )
    return 1
