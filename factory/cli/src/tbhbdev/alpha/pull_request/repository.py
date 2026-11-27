# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Resolve the destination repository, default branch, and visibility."""

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.metadata import (
    GITHUB_REMOTE,
    NAME_WITH_OWNER,
    remote_head,
    repository_name,
    visibility_of,
)
from tbhbdev.alpha.pull_request.tools import GH


def default_base(*, origin_only: bool = False) -> str:
    """The repository's default branch: the remote's own HEAD, else origin/HEAD.

    The remote answers first because origin/HEAD goes stale when the default
    branch moves and a plain fetch leaves it, and a fresh mirror may have none.

    Raises:
        SystemExit: ``GH_REPO`` overrides origin and ``origin_only`` is false,
            or neither read answers.
    """
    hint = "; name the base in the draft's frontmatter"
    if not origin_only and os.environ.get("GH_REPO"):
        message = f"cannot find the default branch of GH_REPO{hint}"
        raise SystemExit(message)
    code, out = pr_tools.git("ls-remote", "--symref", "origin", "HEAD")
    if code == 0 and (branch := remote_head(out)):
        return branch
    code, out = pr_tools.git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    name = out.removeprefix("refs/remotes/origin/")
    if code == 0 and name and name != out:
        return name
    message = (
        "cannot find the default branch: `git ls-remote --symref origin HEAD` "
        f"failed and origin/HEAD is not a symbolic ref{hint}"
    )
    raise SystemExit(message)


def base_of(meta: Mapping[str, object], default: str | None = None) -> str:
    """The PR's base branch: the frontmatter's, else DEFAULT or the repository's."""
    if "base" in meta:
        return str(meta["base"])
    return default if default is not None else default_base()


def repo_visibility(target: str) -> str:
    """The visibility of ``target``, an ``owner/name``, as gh reports it.

    This is the visibility of the repository the text is posted to, not the
    working directory's. An empty, missing or malformed target returns ""
    without a gh call, and so does a failed read. An empty value scans under the
    full private config.
    """
    if not NAME_WITH_OWNER.fullmatch(target):
        return ""
    argv = [
        pr_tools.find_tool(GH),
        "repo",
        "view",
        target,
        "--json",
        "visibility",
        "--jq",
        ".visibility",
    ]
    done = pr_tools.run(argv)
    return done.stdout.strip() if done.returncode == 0 else ""


def base_visibility() -> str:
    """The visibility of the repository ``gh pr create`` will post to, or "".

    No pull request exists yet, so this is the base repository. gh resolves it
    for ``gh repo view`` with no argument the same way it does for
    ``gh pr create``, so one read of ``nameWithOwner`` and ``visibility`` gives
    both. The visibility counts only if ``nameWithOwner`` parses as
    ``owner/name``; a failed or malformed read returns "".
    """
    done = pr_tools.run(
        [pr_tools.find_tool(GH), "repo", "view", "--json", "nameWithOwner,visibility"]
    )
    data = pr_github.gh_data(done)
    return visibility_of(data)


def origin_repository(*, allow_push_fallback: bool = False) -> str:
    """Resolve origin's fetch URL, independent of gh's default repository.

    For the size check, a mirror fetch URL can use origin's push URL for
    repository identity. Git still fetches the base through origin.

    Raises:
        SystemExit: origin or its GitHub repository can't be read.
    """
    code, url = pr_tools.git("remote", "get-url", "origin")
    if allow_push_fallback and code == 0 and url and not GITHUB_REMOTE.match(url):
        code, url = pr_tools.git("remote", "get-url", "--push", "origin")
    if code or not url:
        message = "cannot read origin"
        raise SystemExit(message)
    done = pr_tools.run(
        [pr_tools.find_tool(GH), "repo", "view", url, "--json", "nameWithOwner,url"]
    )
    data = pr_github.gh_data(done)
    return repository_name(data)


def target_is_origin() -> bool:
    """Whether ``gh pr create`` posts to origin's repository.

    gh's default repository (``gh repo set-default``, or a parent fork) may
    differ from origin, and ``GH_REPO`` overrides both. Any failed read counts
    as "no", so the size check refuses to count against the wrong base.
    """
    if os.environ.get("GH_REPO"):
        return False  # gh pr create posts to GH_REPO, not to its default repository
    data = pr_github.gh_data(
        pr_tools.run(
            [pr_tools.find_tool(GH), "repo", "view", "--json", "nameWithOwner"]
        )
    )
    target = data.get("nameWithOwner") if isinstance(data, dict) else None
    try:
        origin = origin_repository(allow_push_fallback=True)
    except SystemExit:
        return False
    return isinstance(target, str) and target.casefold() == origin.casefold()
