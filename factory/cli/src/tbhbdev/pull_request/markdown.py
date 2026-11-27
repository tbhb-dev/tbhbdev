# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Scan pull request Markdown and count prose words."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

HEADING = re.compile(r" {0,3}(#{1,6}) +(\S.*?) *")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
INLINE_CODE = re.compile(r"(`+).+?\1")
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:.*$", re.MULTILINE)
URL = re.compile(r"<?https?://[^\s>]*>?")


@dataclass(frozen=True)
class Line:
    """One draft line: its number, its raw text, and what analysis sees of it.

    ``text`` has HTML comments removed and is empty inside a fenced block,
    where ``code`` is True.
    """

    no: int
    raw: str
    text: str
    code: bool


@dataclass(frozen=True)
class Block:
    """An H2 section: its heading line and the lines up to the next H2."""

    heading: str
    no: int
    lines: list[Line]


def count_words(text: str) -> int:
    """Count prose words: code, link targets, and comments are free.

    A word is a whitespace-separated token with a letter or digit in it, so
    list markers and bare punctuation are not words.
    """
    text = COMMENT.sub(" ", text)
    text = INLINE_CODE.sub(" ", text)
    text = REF_DEF.sub(" ", text)
    text = LINK.sub(r" \1 ", text)
    text = REF_LINK.sub(r" \1 ", text)
    text = URL.sub(" ", text)
    return sum(any(char.isalnum() for char in token) for token in text.split())


def fence_flags(raw: Sequence[str]) -> tuple[list[bool], int | None]:
    """Flag the lines inside fenced code, delimiters included.

    Also returns the index of the opening fence that is never closed, if any.
    """
    flags: list[bool] = []
    marker = ""  # pragma: no mutate -- None is as falsy
    opened = 0  # pragma: no mutate -- read only once marker is set
    for index, line in enumerate(raw):
        match = FENCE.match(line)
        if not marker:
            if match:
                marker, opened = match[1], index
            flags.append(bool(marker))
            continue
        flags.append(True)
        if match and set(match[1]) == set(marker) and len(match[1]) >= len(marker):
            marker = "" if line.strip() == match[1] else marker
    return flags, (opened if marker else None)


def strip_comments(texts: Sequence[str]) -> tuple[list[str], list[int]]:
    """Blank out HTML comments, keeping line numbers.

    Returns the lines and the index of the line each comment starts on.
    """
    joined = "\n".join(texts)
    starts = [joined[: match.start()].count("\n") for match in COMMENT.finditer(joined)]
    clean = COMMENT.sub(lambda match: "\n" * match[0].count("\n"), joined)
    return clean.split("\n")[: len(texts)], starts


def scan(raw: Sequence[str], first: int) -> tuple[list[Line], list[tuple[int, str]]]:
    """Turn raw body lines (the first is line ``first``) into analysis lines."""
    flags, unclosed = fence_flags(raw)
    texts, comments = strip_comments(
        ["" if flags[index] else line for index, line in enumerate(raw)]
    )
    problems = [
        (first + index, "HTML comment left in the description") for index in comments
    ]
    if unclosed is not None:
        problems.append((first + unclosed, "code fence is never closed"))
    lines = [
        Line(first + index, line, texts[index], flags[index])
        for index, line in enumerate(raw)
    ]
    return lines, problems


def heading_of(line: Line) -> tuple[int, str] | None:
    """The level and text of a heading line, or None."""
    match = None if line.code else HEADING.fullmatch(line.text)
    return (len(match[1]), match[2]) if match else None


def split_blocks(
    lines: Sequence[Line],
) -> tuple[list[Line], list[Block], list[tuple[int, str]]]:
    """Split the lines after the title into the preamble and the H2 sections."""
    preamble: list[Line] = []
    blocks: list[Block] = []
    problems: list[tuple[int, str]] = []
    for line in lines:
        found = heading_of(line)
        if found and found[0] == 1:
            problems.append((line.no, "only one H1 is allowed: the title"))
        if found and found[0] == 2:  # noqa: PLR2004 -- H2 is the section level
            blocks.append(Block(found[1], line.no, []))
        elif blocks:
            blocks[-1].lines.append(line)
        else:
            preamble.append(line)
    return preamble, blocks, problems


def is_blank(line: Line) -> bool:
    """True for a line with no text and no code."""
    return not line.code and not line.text.strip()
