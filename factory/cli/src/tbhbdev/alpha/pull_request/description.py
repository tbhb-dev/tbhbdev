# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse and format pull request descriptions and trailers."""

import re
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.identity.records import as_dict
from tbhbdev.alpha.identity.trailers import merge_pr_trailers

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from tbhbdev.alpha.pull_request.draft import Checked


OPENED_KEY = "Opened-by"


# The line create writes, and any spelling of the key in a draft.
OPENED_LINE = re.compile(rf"{OPENED_KEY}: ")


def body_of(rendered: Sequence[str]) -> str:
    """The description: everything after the title, trimmed."""
    return "\n".join(rendered[1:]).strip("\n") + "\n"


def trailer_lines(text: str, llm_line: re.Pattern[str]) -> list[tuple[str, str]]:
    """Match LLM trailer lines in Git's parsed trailer output."""
    found = (llm_line.match(line) for line in text.splitlines())
    return [(match[1], match[2]) for match in found if match]


def commit_messages(pages: Sequence[object], invalid: str) -> list[str]:
    """Read the commit messages in the order GitHub returned them."""
    messages: list[str] = []
    for page in pages:
        if not isinstance(page, list):
            raise SystemExit(invalid)
        for entry in cast("list[object]", page):  # pragma: no mutate
            item = as_dict(entry) or {}
            commit = as_dict(item.get("commit")) or {}
            message = commit.get("message")
            if not isinstance(message, str):
                raise SystemExit(invalid)
            messages.append(message)
    return messages


def opened_line(body: str) -> str | None:
    """The ``Opened-by`` line of a PR body's final paragraph, or None."""
    paragraph: list[str] = []
    for line in reversed(body.splitlines()):
        if line.strip():
            paragraph.append(line)
        elif paragraph:
            break
    return next((line for line in paragraph if OPENED_LINE.match(line)), None)


def description(
    checked: Checked,
    posting: str | None,
    found: Sequence[tuple[str, str]],
    opened_by: str | None = None,
    *,
    check_value: Callable[[str], str | None],
) -> str:
    """The final body: the description plus the trailers as its last paragraph.

    ``opened_by``, when given, is the paragraph's first line.

    Raises:
        SystemExit: A trailer, or the posting agent's, fails the commit-trailer
            grammar.
    """
    values = [value for _, value in found] + ([posting] if posting else [])
    errors = [f"{value}: {why}" for value in values if (why := check_value(value))]
    if errors:
        raise SystemExit("invalid LLM trailer: " + "; ".join(errors))
    merged = [f"{key}: {value}" for key, value in merge_pr_trailers(found, posting)]
    trailers = ([opened_by] if opened_by else []) + merged
    body = body_of(checked.rendered)
    if not trailers:
        return body
    return body + "\n" + "\n".join(trailers) + "\n"
