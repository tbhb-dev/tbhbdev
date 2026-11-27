# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse GitHub repository names and build pull request flags."""

import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping


# A repository as ``owner/name``: GitHub's characters for each part, and a name
# that is not "." or "..". The owner can't begin with "-", so gh can't read it
# as a flag.
PR_URL = re.compile(r"https://[^/\s]+/([^/\s]+/[^/\s]+)/pull/(\d+)")

REPO = r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.+(?:/|\Z))[A-Za-z0-9_.-]+"
NAME_WITH_OWNER = re.compile(REPO)
GITHUB_REMOTE = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://(?:[^/@\s]+@)?(?:ssh\.)?github\.com(?::\d+)?/"
    r"|(?:[^@/:\s]+@)?github\.com:)",
    re.IGNORECASE,
)


PR_PAGE = re.compile(rf"https://github\.com/({REPO})/pull/[0-9]+")


def target_of_pr(url: object) -> str:
    """The ``owner/name`` in a pull request's github.com URL, or "".

    The URL must be a string of exactly
    ``https://github.com/<owner>/<name>/pull/<digits>``; anything else, a
    missing URL included, names nothing.
    """
    found = PR_PAGE.fullmatch(url) if isinstance(url, str) else None
    return found[1] if found else ""


def gh_args(meta: Mapping[str, object], base: str) -> list[str]:
    """The gh pr create flags the frontmatter asks for."""
    args = ["--base", base]
    if meta.get("draft"):
        args.append("--draft")
    for flag, key in (
        ("--label", "labels"),
        ("--reviewer", "reviewers"),
        ("--assignee", "assignees"),
    ):
        names = cast("list[str]", meta.get(key, []))  # pragma: no mutate
        args += [item for name in names for item in (flag, name)]
    if "milestone" in meta:
        args += ["--milestone", str(meta["milestone"])]
    return args


def remote_head(output: str) -> str | None:
    """The branch advertised as the remote's symbolic HEAD."""
    found = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", output, re.MULTILINE)
    return found[1] if found else None


def repository_name(data: object) -> str:
    """Require an unambiguous GitHub repository name and URL."""
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or not isinstance(name, str)
        or not NAME_WITH_OWNER.fullmatch(name)
    ):
        message = "cannot resolve origin's GitHub repository"
        raise SystemExit(message)
    if data.get("url") != f"https://github.com/{name}":
        message = "origin must name a repository on github.com"
        raise SystemExit(message)
    return name


def visibility_of(data: object) -> str:
    """Visibility of a valid repository response, or an empty string."""
    if not isinstance(data, dict):
        return ""
    name = data.get("nameWithOwner")
    if not (isinstance(name, str) and NAME_WITH_OWNER.fullmatch(name)):
        return ""
    return str(data.get("visibility") or "")


def pr_view_args(number: int, fields: str, *, repo: str = "") -> list[str]:
    """Build the arguments to read selected fields of a pull request."""
    target = ["--repo", f"github.com/{repo}"] if repo else []
    return ["pr", "view", str(number), *target, "--json", fields]
