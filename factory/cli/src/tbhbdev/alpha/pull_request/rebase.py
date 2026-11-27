# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Request a rebase on an open Renovate pull request."""

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import repository as pr_repository
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.branch_rules import rebase_body
from tbhbdev.alpha.pull_request.tools import GH

RENOVATE_AUTHOR = "tbhb-dev-renovate[bot]"
RENOVATE_APP = "app/tbhb-dev-renovate"


def renovate_author(login: object, number: int, repo: str) -> bool:
    """Check the exact login, resolving gh's display name for the App."""
    if login == RENOVATE_AUTHOR:
        return True
    if login != RENOVATE_APP:
        return False
    done = pr_tools.run_bytes(
        [
            pr_tools.find_tool(GH),
            "api",
            "--hostname",
            "github.com",
            f"repos/{repo}/pulls/{number}",
            "--jq",
            ".user.login",
        ]
    )
    return done.returncode == 0 and done.stdout == RENOVATE_AUTHOR.encode() + b"\n"


def rebase(numbers: Sequence[int]) -> int:
    """Ask Renovate to rebase each open pull request, stopping at the first failure.

    Raises:
        SystemExit: The pull request or its checkbox cannot be safely updated.
    """
    repo = pr_repository.origin_repository()
    for number in numbers:
        target = [str(number), "--repo", f"github.com/{repo}"]
        data = pr_github.gh_data(
            pr_tools.run_bytes(
                [
                    pr_tools.find_tool(GH),
                    "pr",
                    "view",
                    *target,
                    "--json",
                    "author,state,body",
                ]
            )
        )
        if not isinstance(data, dict):
            msg = f"PR {number}: cannot read pull request on origin"
            raise SystemExit(msg)
        author: object = data.get("author")
        login: object = author.get("login") if isinstance(author, dict) else None
        if not renovate_author(login, number, repo):
            msg = f"PR {number}: author is not {RENOVATE_AUTHOR}"
            raise SystemExit(msg)
        if data.get("state") != "OPEN":
            msg = f"PR {number}: pull request is not open"
            raise SystemExit(msg)
        body: object = data.get("body")
        if not isinstance(body, str):
            msg = f"PR {number}: cannot read pull request body"
            raise SystemExit(msg)
        updated = rebase_body(number, body)
        if updated is None:
            print(f"PR {number}: the Renovate rebase checkbox is already ticked")
            continue
        status, _ = pr_github.invoke_gh(
            [GH, "pr", "edit", *target, "--body-file", "-"], updated, quiet=True
        )
        if status:
            print(
                f"PR {number}: could not tick the Renovate rebase checkbox",
                file=sys.stderr,
            )
            return status
        print(f"PR {number}: ticked the Renovate rebase checkbox")
    return 0
