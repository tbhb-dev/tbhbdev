# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Assemble and validate the subject, body and trailers of a merge commit."""

from dataclasses import dataclass
from itertools import takewhile
from typing import TYPE_CHECKING

from tbhbdev.alpha.merge.body import MarkdownLine

if TYPE_CHECKING:
    from collections.abc import Sequence
    from re import Pattern

DEFAULT_MERGE_TITLES = ("Merge pull request", "Merge branch")


@dataclass(frozen=True)
class Message:
    """A commit message: the subject and the body that follows it."""

    subject: str
    body: str

    def text(self) -> str:
        """The whole message as git stores it."""
        return f"{self.subject}\n\n{self.body}" if self.body else f"{self.subject}\n"


def is_blank(line: MarkdownLine) -> bool:
    """Whether a scanned line has no text or fenced code."""
    return not line.code and not line.text.strip()


def drop_opened[T: MarkdownLine](
    lines: Sequence[T], trailer: Pattern[str], opened: Pattern[str]
) -> list[T]:
    """The lines without the ``Opened-by`` line of the final trailer paragraph.

    The paragraph is the trailing run of LLM trailer and ``Opened-by`` lines
    after a blank line. Anywhere else, ``Opened-by`` stays for
    ``check_trailers`` to refuse.
    """
    end = len(lines)
    while end and is_blank(lines[end - 1]):
        end -= 1
    run = takewhile(
        lambda line: trailer.match(line.text) or opened.match(line.text),
        reversed(lines[:end]),
    )
    start = end - len(list(run))
    if start and not is_blank(lines[start - 1]):
        return list(lines)
    return [
        line
        for index, line in enumerate(lines)
        if not (start <= index and opened.match(line.text))
    ]


def split_trailers[T: MarkdownLine](
    lines: Sequence[T], trailer: Pattern[str]
) -> tuple[list[T], list[str]]:
    """Split off the body's final paragraph when it is all LLM trailers."""
    end = len(lines)
    while end and is_blank(lines[end - 1]):
        end -= 1
    start = end
    while start and not lines[start - 1].code and trailer.match(lines[start - 1].text):
        start -= 1
    if start == end or (start and not is_blank(lines[start - 1])):
        return list(lines), []
    return list(lines[:start]), [line.raw.rstrip() for line in lines[start:end]]


def check_trailers(
    trailers: Sequence[str],
    stray: Sequence[str],
    invalid: Sequence[str],
    *,
    agent: bool,
) -> None:
    """Refuse stray or invalid trailers and an agent PR's missing final block."""
    problems = [*stray, *(f"invalid trailer block: {why}" for why in invalid)]
    if agent and not trailers:
        problems.append("agent PR is missing final trailer paragraph")
    if problems:
        message = "the PR body cannot be merged as is: " + "; ".join(problems)
        error = message + "; add a valid trailer paragraph with tbhbdev alpha pr edit"
        raise SystemExit(error)


def check_subject(subject: str, problems: Sequence[str]) -> None:
    """Report all subject problems, including the result of the commit linter."""
    if problems:
        error = f"the commit subject {subject!r} fails: {'; '.join(problems)}"
        raise SystemExit(error)


def check_round_trip(parsed: str, trailers: Sequence[str]) -> None:
    """Require git's parsed trailer block to match the trailers that were written."""
    if parsed != "\n".join(trailers) + "\n":
        reason = f"git interpret-trailers --parse read back: {parsed!r}"
        error = f"the trailer block did not survive the message: {reason}"
        raise SystemExit(error)


def assemble(subject: str, parts: Sequence[str], trailers: Sequence[str]) -> Message:
    """Join the rendered sections and add the trailers as the final block."""
    blocks = [*parts, "\n".join(trailers)] if trailers else parts
    return Message(subject, "\n\n".join(blocks) + "\n" if blocks else "")


def require_clean_message(message: Message, number: int) -> None:
    """Refuse a merge that would get GitHub's default message."""
    if not message.body or message.subject.startswith(DEFAULT_MERGE_TITLES):
        error = (
            f"not merging PR #{number}: the merge commit would get "
            "GitHub's default message, not the cleaned PR body"
        )
        raise SystemExit(error)
