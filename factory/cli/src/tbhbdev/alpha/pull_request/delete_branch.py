# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Delete the unprotected origin branch of a finished pull request."""

from urllib.parse import quote

from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import repository as pr_repository
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.branch_rules import (
    branch_to_delete,
    require_unprotected,
)
from tbhbdev.alpha.pull_request.metadata import pr_view_args
from tbhbdev.alpha.pull_request.tools import GH


def closed_pr_head(number: int, repo: str) -> str:
    """Read the head of a finished PR whose branch belongs to origin.

    Raises:
        SystemExit: The PR can't be read, isn't finished or has an unsafe head.
    """
    done = pr_tools.run(
        [
            pr_tools.find_tool(GH),
            *pr_view_args(number, "state,headRefName,isCrossRepository", repo=repo),
        ]
    )
    data = pr_github.gh_data(done)
    branch = branch_to_delete(number, data)
    code, _ = pr_tools.git("check-ref-format", f"refs/heads/{branch}")
    if code:
        message = f"invalid head branch of PR {number}"
        raise SystemExit(message)
    return branch


def delete_pr_branch(number: int) -> int:
    """Delete a finished PR's unprotected head branch from origin only.

    Raises:
        SystemExit: The PR or origin is unsafe, or protection can't be read.
    """
    repo = pr_repository.origin_repository()
    branch = closed_pr_head(number, repo)
    ref = quote(branch, safe="")
    done = pr_tools.run(
        [
            pr_tools.find_tool(GH),
            "api",
            "--hostname",
            "github.com",
            f"repos/{repo}/branches/{ref}",
        ]
    )
    data = pr_github.gh_data(done)
    require_unprotected(branch, data)
    return pr_github.invoke_gh(
        [
            GH,
            "api",
            "--hostname",
            "github.com",
            "--method",
            "DELETE",
            f"repos/{repo}/git/refs/heads/{ref}",
        ],
        "",
    )[0]
