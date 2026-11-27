"""Find citations in review drafts and anchor them to a compared diff."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from markdown_it import MarkdownIt

_LOCATION = re.compile(r"(.+):([1-9][0-9]{0,9})")
_HUNK = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    body: str


def findings(body: str) -> list[Finding]:
    """Read the first backticked path and line from each Findings bullet."""
    tokens = iter(MarkdownIt().parse(body))
    for token in tokens:
        if token.type == "heading_open" and next(tokens).content == "Findings":
            break
    else:
        return []

    found = []
    for token in tokens:
        if token.type == "heading_open":
            break
        if token.type != "inline":
            continue
        citations = (
            match
            for inline in token.children or ()
            if inline.type == "code_inline"
            and (match := _LOCATION.fullmatch(inline.content))
        )
        if location := next(citations, None):
            found.append(Finding(location[1], int(location[2]), token.content))
    return found


def _right_lines(patch: str) -> set[int]:
    lines: set[int] = set()
    line = 0
    for row in patch.split("\n"):
        if hunk := _HUNK.match(row):
            line = int(hunk[1])
        elif line and row.startswith(("+", " ")):
            lines.add(line)
            line += 1
    return lines


def comments(found: list[Finding], diff: object) -> list[dict[str, str | int]]:
    """Build comments for citations to added or context lines in compared files."""
    lines: dict[str, set[int]] = {}
    match cast("dict[str, object] | None", diff):
        case {"files": list(files)}:
            for file in cast("list[object]", files):
                match cast("dict[str, object] | None", file):
                    case {"filename": str(path), "patch": str(patch)}:
                        lines[path] = _right_lines(patch)
    return [
        {
            "path": finding.path,
            "line": finding.line,
            "side": "RIGHT",
            "body": finding.body,
        }
        for finding in found
        if finding.line in lines.get(finding.path, ())
    ]
