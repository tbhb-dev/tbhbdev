# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse Markdown and validate pull request body sections."""

import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

type Problem = tuple[int, str]


HEADING = re.compile(r" {0,3}(#{1,6}) +(\S.*?) *")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
INLINE_CODE = re.compile(r"(`+).+?\1")
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:.*$", re.MULTILINE)
URL = re.compile(r"<?https?://[^\s>]*>?")
HUMAN_TRAILER = re.compile(r"(?:Signed-off-by|Co-authored-by):", re.IGNORECASE)
DRAFT_OPENED = re.compile(r"\s*opened-by\s*:", re.IGNORECASE)
OPENED_MESSAGE = "the script writes Opened-by; remove this line"
LIST_ITEM = re.compile(r" *(?:[-*+]|\d+[.)]) +\S")


# A ``#N`` that GitHub links to this repository's issue or pull request N. One
# written ``owner/repo#N`` has a name character before the ``#``.
BARE_REF = re.compile(r"(?<![\w./&-])#(\d+)\b")
BARE_REF_MESSAGE = (
    "#{number} links to this repository's #{number}; "
    "write owner/repo#{number}, even for this repository"
)


HUMAN_TRAILER_MESSAGE = (
    "Signed-off-by and Co-authored-by do not belong in the description; "
    "remove this line"
)


WRAPPED = (
    "paragraph is hard-wrapped; write each paragraph on one line "
    "(GitHub renders newlines as line breaks)"
)


@dataclass(frozen=True)
class Section:
    """One H2 section of the description, from the sections file."""

    heading: str
    required: bool
    max_words: int | None


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


def parse_sections(text: str) -> list[Section]:
    """Parse the ordered section scheme."""
    data = tomllib.loads(text)
    rows = cast("list[dict[str, object]]", data["section"])  # pragma: no mutate
    return [
        Section(
            str(row["heading"]),
            row["required"] is True,
            cast("int | None", row.get("max_words")),  # pragma: no mutate
        )
        for row in rows
    ]


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


def scan(raw: Sequence[str], first: int) -> tuple[list[Line], list[Problem]]:
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
) -> tuple[list[Line], list[Block], list[Problem]]:
    """Split the lines after the title into the preamble and the H2 sections."""
    preamble: list[Line] = []
    blocks: list[Block] = []
    problems: list[Problem] = []
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


def block_problems(
    blocks: Sequence[Block], sections: Sequence[Section]
) -> list[Problem]:
    """Check section names, order, emptiness, and word limits."""
    order = {section.heading: index for index, section in enumerate(sections)}
    problems: list[Problem] = []
    last = -1  # pragma: no mutate -- indexes start at 0, so -2 behaves alike
    for block in blocks:
        index = order.get(block.heading)
        if index is None:
            known = ", ".join(order)
            problems.append(
                (block.no, f"unknown section '{block.heading}'; use: {known}")
            )
            continue
        if index <= last:
            problems.append(
                (block.no, f"section '{block.heading}' is duplicated or out of order")
            )
        last = max(last, index)
        if all(map(is_blank, block.lines)):
            problems.append((block.no, f"section '{block.heading}' is empty"))
        limit = sections[index].max_words
        words = count_words("\n".join(line.text for line in block.lines))
        if limit is not None and words > limit:
            problems.append(
                (
                    block.no,
                    f"section '{block.heading}' has {words} words (limit {limit})",
                )
            )
    return problems


def wrap_problems(lines: Sequence[Line], llm_line: re.Pattern[str]) -> list[Problem]:
    """Flag each paragraph or list item that continues onto a second line.

    GitHub renders a newline in a description as a line break, so wrapped prose
    shows as ragged short lines. Code, tables, headings, blank lines, and the
    start of a list item end a run, so each list item stays on its own line.
    """
    problems: list[Problem] = []
    start: Line | None = None  # first line of the prose run being read
    reported = False
    for line in lines:
        text = "" if line.code else line.text.strip()
        if (
            not text
            or HEADING.fullmatch(line.text)
            or text.startswith("|")
            or HUMAN_TRAILER.match(text)
            or llm_line.match(text)
        ):
            start = None
        elif start is None or LIST_ITEM.match(line.text):
            start, reported = line, False
        elif not reported:
            problems.append((start.no, WRAPPED))
            reported = True
    return problems


def trailer_problems(lines: Sequence[Line], llm_line: re.Pattern[str]) -> list[Problem]:
    """Reject trailers in the draft: the script writes the LLM ones.

    Signed-off-by and Co-authored-by are rejected for everyone: the merge
    commit's author is the person who merges, and the script never writes them.
    """
    problems: list[Problem] = []
    for line in lines:
        if DRAFT_OPENED.match(line.raw):
            problems.append((line.no, OPENED_MESSAGE))
            continue
        if line.code:
            continue
        if llm_line.match(line.text):
            problems.append(
                (
                    line.no,
                    "the script writes Generated-by and Assisted-by; remove this line",
                )
            )
        elif HUMAN_TRAILER.match(line.text):
            problems.append((line.no, HUMAN_TRAILER_MESSAGE))
    return problems


def ref_problems(lines: Sequence[Line]) -> list[Problem]:
    """Reject ``#N`` references that don't name their repository.

    GitHub links a bare ``#N`` to the repository it appears in, which is wrong
    for another repository's pull request and ambiguous once the merge commit
    is read elsewhere, so every reference is ``owner/repo#N``. Code and links
    are exempt: neither becomes a reference.
    """
    problems: list[Problem] = []
    for line in lines:
        if line.code:
            continue
        text = INLINE_CODE.sub(" ", line.text)
        text = LINK.sub(" ", text)
        text = REF_LINK.sub(" ", text)
        text = URL.sub(" ", text)
        problems += [
            (line.no, BARE_REF_MESSAGE.format(number=number))
            for number in BARE_REF.findall(text)
        ]
    return problems


def body_problems(
    lines: Sequence[Line],
    anchor: Line,
    sections: Sequence[Section],
    llm_line: re.Pattern[str],
) -> list[Problem]:
    """Check sections, stray text, and trailers; anchor missing sections here."""
    preamble, blocks, problems = split_blocks(lines)
    stray = next((line for line in preamble if not is_blank(line)), None)
    if stray:
        problems.append(
            (stray.no, "text before the first section; put it under a heading")
        )
    problems += block_problems(blocks, sections)
    problems += wrap_problems(lines, llm_line)
    present = {block.heading for block in blocks}
    problems += [
        (anchor.no, f"missing required section '{section.heading}'")
        for section in sections
        if section.required and section.heading not in present
    ]
    return problems + trailer_problems(lines, llm_line)
