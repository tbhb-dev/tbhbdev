# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Render Markdown from a pull request as plain commit message text."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

WIDTH = 72
CODE_INDENT = "    "
TOKEN = re.compile(r"(?:(`+).+?\1|\S)+")
ITEM = re.compile(r"( *)([-*+]|\d+[.)]) +(\S.*)")
DELIMITER = re.compile(r"\|?(?: *:?-+:? *\|)* *:?-+:? *\|?")
MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?: +\"[^\"]*\")?\)")
AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
EMPHASIS = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
PLACEHOLDER = re.compile("\x00(\\d+)\x00")


class MarkdownLine(Protocol):
    """A line from the pull request Markdown parser."""

    @property
    def raw(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def code(self) -> bool: ...


@dataclass(frozen=True)
class Line:
    """A Markdown line with any heading or fence marker already identified."""

    raw: str
    text: str
    code: bool
    heading: str = ""
    fence: str = ""


@dataclass(frozen=True)
class Syntax[Source: MarkdownLine]:
    """Markdown patterns and heading detection from the pull request parser."""

    fence: re.Pattern[str]
    inline_code: re.Pattern[str]
    heading_of: Callable[[Source], tuple[int, str] | None]

    def render_line(self, source: Source) -> Line:
        """Supply the parser's heading and fence information to the renderer."""
        heading = self.heading_of(source)
        marker = self.fence.match(source.raw) if source.code else None
        return Line(
            source.raw,
            source.text,
            source.code,
            heading[1] if heading else "",
            marker[1] if marker else "",
        )


def wrap(text: str, first: str, rest: str, width: int = WIDTH) -> list[str]:
    """Wrap ``text`` greedily; ``first`` and ``rest`` start the first and later lines.

    A token (a word, a URL, or a code span with its neighbors) is never split,
    so a token wider than the line gets a line of its own.
    """
    lines: list[str] = []
    current = first
    empty = True
    for match in TOKEN.finditer(text):
        token = match[0]
        if empty:
            current += token
        elif len(current) + 1 + len(token) <= width:
            current += " " + token
        else:
            lines.append(current)
            current = rest + token
        empty = False
    return [*lines, current]


def reference(url: str, refs: dict[str, int]) -> str:
    """The marker for a URL, numbering it on first use."""
    return f"[{refs.setdefault(url, len(refs) + 1)}]"


def prose(text: str, refs: dict[str, int]) -> str:
    """Plain text for Markdown prose: links to references, emphasis dropped."""

    def link(match: re.Match[str]) -> str:
        label, url = match[2], match[3]
        return url if label == url else f"{label} {reference(url, refs)}".strip()

    text = MD_LINK.sub(link, text)
    text = AUTOLINK.sub(r"\1", text)
    text = STRONG.sub(r"\2", text)
    return EMPHASIS.sub(r"\1", text)


def inline(text: str, refs: dict[str, int], inline_code: re.Pattern[str]) -> str:
    """Plain text for a Markdown line, with inline code left exactly as written."""
    spans: list[str] = []

    def hold(match: re.Match[str]) -> str:
        spans.append(match[0])
        return f"\x00{len(spans) - 1}\x00"

    held = inline_code.sub(hold, text)
    return PLACEHOLDER.sub(lambda m: spans[int(m[1])], prose(held, refs))


@dataclass
class Pending:
    """Text lines collected for one paragraph or list item."""

    first: str
    rest: str
    parts: list[str]


def close_fence(line: Line, marker: str) -> bool:
    """True when the line closes a fence opened with ``marker``."""
    return (
        line.raw.strip() == line.fence
        and line.fence.startswith(marker[0])
        and len(line.fence) >= len(marker)
    )


@dataclass
class Renderer:
    """Turns section lines into blocks of plain text, one string per block."""

    refs: dict[str, int]
    blocks: list[str]
    inline_code: re.Pattern[str]
    pending: Pending | None = None
    code: list[str] | None = None
    marker: str = ""
    # The last block is a list item with no blank line after it, so the next
    # item joins that block instead of starting one.
    in_list: bool = False
    # The last line taken was a table row, so the next row joins its block.
    in_table: bool = False

    def flush(self) -> None:
        """End the paragraph or list item being collected."""
        if self.pending is not None:
            text = inline(" ".join(self.pending.parts), self.refs, self.inline_code)
            lines = "\n".join(wrap(text, self.pending.first, self.pending.rest))
            item = bool(self.pending.first)
            if item and self.in_list:
                self.blocks[-1] += "\n" + lines
            else:
                self.blocks.append(lines)
            self.in_list = item
        self.pending = None

    def feed_code(self, line: Line, code: list[str]) -> None:
        """Take a line inside a fence: indent it, or close the block."""
        if close_fence(line, self.marker):
            self.blocks.append("\n".join(code))
            self.code = None
        else:
            code.append(CODE_INDENT + line.raw if line.raw.strip() else "")

    def feed_row(self, row: str) -> None:
        """Take a table row: add it to the table's block, as written."""
        self.flush()
        self.in_list = False  # pragma: no mutate -- None is as falsy
        if self.in_table:
            self.blocks[-1] += "\n" + row
        else:
            self.blocks.append(row)

    def feed(self, line: Line, row: bool) -> None:  # noqa: FBT001
        """Take the next line of the section; ``row`` marks a table row."""
        if row:
            self.feed_row(line.text.strip())
        else:
            self.feed_other(line)
        self.in_table = row

    def feed_other(self, line: Line) -> None:
        """Take a line that is not a table row."""
        if self.code is not None:
            self.feed_code(line, self.code)
        elif line.code:
            self.flush()
            self.in_list = False  # pragma: no mutate -- None is as falsy
            self.marker = line.fence
            self.code = []
        elif not line.text.strip():
            self.flush()
            self.in_list = False  # pragma: no mutate -- None is as falsy
        elif item := ITEM.fullmatch(line.text):
            self.flush()
            prefix = f"{item[1]}{item[2]} "
            self.pending = Pending(prefix, " " * len(prefix), [item[3]])
        elif self.pending is None:
            text = line.heading or line.text.strip()
            self.pending = Pending("", "", [text])
        else:
            self.pending.parts.append(line.text.strip())


def table_rows(lines: Sequence[Line]) -> list[bool]:
    """Flag each line that is a row of a Markdown table.

    A table is a ``|`` header row, then a delimiter row such as ``|---|``,
    then the ``|`` body rows up to the first other line. Any other line that
    starts with ``|`` is prose. Lines inside a fence have no text, so they are
    never rows.
    """
    texts = [line.text.strip() for line in lines]
    rows = [False] * len(texts)
    for index, text in enumerate(texts):
        if not text.startswith("|"):
            continue
        # At index 0, rows[-1] is still False: no later line is flagged yet.
        if rows[index - 1]:
            rows[index] = True
        elif index + 1 < len(texts) and DELIMITER.fullmatch(texts[index + 1]):
            rows[index] = rows[index + 1] = True
    return rows


def render(
    lines: Sequence[Line], refs: dict[str, int], inline_code: re.Pattern[str]
) -> list[str]:
    """Render section lines as blocks of plain text, one string per block."""
    renderer = Renderer(refs, [], inline_code)
    rows = table_rows(lines)
    for index, line in enumerate(lines):
        renderer.feed(line, rows[index])
    renderer.flush()
    return renderer.blocks


def render_body[T: MarkdownLine](
    sections: Sequence[tuple[str, Sequence[T]]], syntax: Syntax[T]
) -> list[str]:
    """Render nonempty sections, then append their numbered link references."""
    refs: dict[str, int] = {}
    parts: list[str] = []
    for heading, lines in sections:
        blocks = render(
            [syntax.render_line(line) for line in lines], refs, syntax.inline_code
        )
        content = "\n\n".join(blocks)
        if heading and blocks:
            parts.append(heading + "\n" + content)
        elif content:
            parts.append(content)
    if refs:
        parts.append("\n".join(f"[{number}]: {url}" for url, number in refs.items()))
    return parts
