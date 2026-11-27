# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Mark an agent-owned pull request ready for review."""

from typing import TYPE_CHECKING

from tbhbdev.alpha.pull_request.metadata import pr_view_args

if TYPE_CHECKING:
    from collections.abc import Sequence
from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import repository as pr_repository
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.branch_rules import own_author_problem
from tbhbdev.alpha.pull_request.tools import GH


def require_own_author(number: int, repo: str) -> None:
    """Refuse unless PR N's author is this ``gh`` account or the pull-requests App.

    A build run opens its own pull request using its GitHub credential, which
    can belong to the pull-requests App.

    Both reads name github.com and ``repo``, the repository ``gh pr ready``
    acts on, so no ``GH_HOST`` or ``GH_REPO`` setting can split them. The two
    logins must match byte for byte. The check isn't atomic with ``gh pr ready``.

    Raises:
        SystemExit: The author or the account can't be read, or they differ.
    """
    author = pr_tools.run_bytes(
        [
            pr_tools.find_tool(GH),
            *pr_view_args(number, "author", repo=repo),
            "--jq",
            ".author.login",
        ]
    )
    me = pr_tools.run_bytes(
        [
            pr_tools.find_tool(GH),
            "api",
            "--hostname",
            "github.com",
            "user",
            "--jq",
            ".login",
        ]
    )
    problem = own_author_problem(
        number,
        author.stdout if author.returncode == 0 else b"",
        me.stdout if me.returncode == 0 else None,
        author.stderr,
        me.stderr,
    )
    if problem:
        raise SystemExit(problem)


def ready(numbers: Sequence[int]) -> int:
    """Mark each pull request ready, in order, stopping at the first failure.

    Raises:
        SystemExit: A pull request's author isn't this agent's account.
    """
    repo = pr_repository.origin_repository()
    for number in numbers:
        require_own_author(number, repo)
        # `gh pr ready` never reads stdin, so an inherited stdin behaves like the
        # empty body: no test can tell `None` from `""`.
        words = [GH, "pr", "ready", str(number), "--repo", f"github.com/{repo}"]
        status = pr_github.invoke_gh(words, "")[0]  # pragma: no mutate
        if status:
            return status
    return 0
