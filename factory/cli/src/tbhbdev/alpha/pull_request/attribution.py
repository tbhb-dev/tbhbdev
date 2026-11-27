# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read pull request attribution from Git, GitHub, and the session."""

import sys
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha import opened
from tbhbdev.alpha.identity import attribution, models
from tbhbdev.alpha.identity.commit import grammar
from tbhbdev.alpha.identity.trailers import format_value

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tbhbdev.alpha.pull_request.draft import Checked
from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.description import (
    OPENED_KEY,
    commit_messages,
    opened_line,
    trailer_lines,
)
from tbhbdev.alpha.pull_request.description import (
    description as format_description,
)
from tbhbdev.alpha.pull_request.tools import GH, GIT


def branch_trailers(base: str) -> list[tuple[str, str]]:
    """The LLM trailers of the commits on this branch, oldest first.

    The range is BASE..HEAD, which starts at the merge base. The PR targets the
    remote branch, so refs/remotes/origin/BASE is the base when it exists, else
    refs/heads/BASE. Full ref names keep a tag named origin/BASE out. Git runs in
    the current directory, because the branch belongs to the repository the
    script is run from.
    """
    remote = f"refs/remotes/origin/{base}"
    code, _ = pr_tools.git("rev-parse", "--verify", "--quiet", f"{remote}^{{commit}}")
    span = f"{remote if code == 0 else f'refs/heads/{base}'}..HEAD"
    code, out = pr_tools.git(
        "log", "--reverse", "--format=%x1e%(trailers:only,unfold)", span
    )
    if code != 0:
        message = f"cannot list the commits in {base}..HEAD; is {base} fetched?"
        raise SystemExit(message)
    return trailer_lines(out, grammar.LINE)


def pr_trailers(number: int, target: str) -> list[tuple[str, str]]:
    """The LLM trailers of the commits GitHub lists for the pull request."""
    problem = f"cannot list the commits of PR {number}"
    if not target:
        error = f"{problem}: cannot determine its GitHub repository"
        raise SystemExit(error)
    endpoint = f"repos/{target}/pulls/{number}/commits"
    api = [
        pr_tools.find_tool(GH),
        "api",
        "--hostname",
        "github.com",
        "--paginate",
        "--slurp",
    ]
    done = pr_tools.run([*api, endpoint])
    pages = pr_github.gh_data(done)
    if not isinstance(pages, list):
        why = " ".join(done.stderr.split()) or "GitHub returned no commit list"
        error = f"{problem}: {why}"
        raise SystemExit(error)
    pages = cast("list[object]", pages)  # pragma: no mutate
    invalid = f"{problem}: GitHub returned invalid commit data"
    messages = commit_messages(pages, invalid)
    if not messages:
        error = f"{problem}: GitHub returned no commits"
        raise SystemExit(error)
    argv = [pr_tools.find_tool(GIT), "interpret-trailers", "--parse", "--no-divider"]
    found: list[tuple[str, str]] = []
    for message in messages:
        parsed = pr_tools.run(argv, stdin=message)
        if parsed.returncode != 0:
            error = f"cannot parse the commit trailers of PR {number}"
            raise SystemExit(error)
        found.extend(trailer_lines(parsed.stdout, grammar.LINE))
    return found


def posting_value(env: Mapping[str, str]) -> str | None:
    """The trailer value for the agent running this script, or None for a human."""
    committer = attribution.committer_identity(env)
    if committer is None:
        return None
    return format_value(committer.ident.attrs())


def live_pr(number: int) -> tuple[str | None, object]:
    """PR N's ``Opened-by`` line on GitHub, or None, and its ``url``.

    The line is the one in the body's final paragraph. The URL is whatever gh
    reports, None when it reports none; ``target_of_pr`` takes it as it is.

    Raises:
        SystemExit: gh can't read the body, so the line can't be kept.
    """
    argv = [pr_tools.find_tool(GH), "pr", "view", str(number), "--json", "body,url"]
    done = pr_tools.run(argv)
    data = pr_github.gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read the body of PR {number} to keep its Opened-by: {why}"
        raise SystemExit(message)
    line = opened_line(str(data.get("body") or ""))  # pragma: no mutate -- no line
    return line, data.get("url")


def opened_by(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """The opener's root hash and ``Opened-by`` line, or (None, None).

    An opener whose value fails the trailer grammar, such as one without a
    model, gets a warning and neither.
    """
    ident = opened.opener(env)
    if ident is None:
        if models.detect(env) is not None:
            message = (
                f"warning: no {OPENED_KEY} line or ledger entry: "
                "cannot resolve the root session"
            )
            print(message, file=sys.stderr)
        return None, None
    value = format_value(ident.attrs())
    if why := grammar.check_value(value):
        message = f"warning: no {OPENED_KEY} line or ledger entry: {value}: {why}"
        print(message, file=sys.stderr)
        return None, None
    return ident.root, f"{OPENED_KEY}: {value}"


def description(
    checked: Checked,
    env: Mapping[str, str],
    found: Sequence[tuple[str, str]],
    opened_by: str | None = None,
) -> str:
    """Render the description using the posting session's attribution."""
    return format_description(
        checked,
        posting_value(env),
        found,
        opened_by,
        check_value=grammar.check_value,
    )
