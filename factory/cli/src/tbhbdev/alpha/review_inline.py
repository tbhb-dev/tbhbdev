"""Split review findings by their anchors in the compared diff."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TypedDict, cast

from tbhbdev.content_model import normalize

_HUNK = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")
_BULLET = re.compile(r"^([ \t]*[-+*][ \t]+)(.*?)(\r\n?|\n)?$")
_NUMBER = re.compile(r"\bF[1-8]\b")
_UNANCHORED = " (not anchored: line not in the diff)"


class _Paragraph(TypedDict):
    source: str
    span: dict[str, dict[str, int]]


@dataclass(frozen=True)
class Finding:
    location: tuple[str, int] | None
    paragraph: str
    offset: int
    visible: str


def findings(body: str) -> list[Finding]:
    """Read the Findings bullets from a validated draft."""
    draft = normalize(body, object="pr-review", variant="review", metadata={})
    blocks = cast("dict[str, dict[str, list[dict[str, object]]]]", draft["blocks"])
    found = []
    for block in blocks.get("findings", {}).get("content", []):
        for item in cast("list[dict[str, object]]", block.get("items", [])):
            paragraph: _Paragraph = cast("list[_Paragraph]", item["blocks"])[0]
            found.append(
                Finding(
                    cast("tuple[str, int] | None", item.get("location")),
                    paragraph["source"],
                    paragraph["span"]["start"]["offset"],
                    cast("str", item["visible"]),
                )
            )
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


def split_findings(
    found: list[Finding], diff: object
) -> tuple[list[Finding], list[Finding]]:
    """Keep citations to right-side diff lines inline and other findings in the body."""
    lines: dict[str, set[int]] = {}
    match cast("dict[str, object] | None", diff):
        case {"files": list(files)}:
            for file in cast("list[object]", files):
                match cast("dict[str, object] | None", file):
                    case {"filename": str(path), "patch": str(patch)}:
                        lines[path] = _right_lines(patch)
                    case _:
                        continue
        case _:
            return [], found
    inline: list[Finding] = []
    remaining: list[Finding] = []
    for finding in found:
        if finding.location and finding.location[1] in lines.get(
            finding.location[0], ()
        ):
            inline.append(finding)
        else:
            remaining.append(finding)
    return inline, remaining


def _count_line(inline: list[Finding], start: int) -> str:
    counts = Counter(
        " ".join(finding.visible.split(maxsplit=3)[1:3]) for finding in inline
    )
    pairs = ", ".join(f"{pair} {count}" for pair, count in sorted(counts.items()))
    last = start + len(inline) - 1
    numbers = f"F{start}" if last == start else f"F{start} to F{last}"
    # A soft break keeps the range in the same paragraph and the count on its own row.
    return f"Inline findings: {len(inline)} ({pairs}).\n{numbers}.\n"


def _bullet(finding: Finding) -> tuple[str, str, str]:
    match = cast("re.Match[str]", _BULLET.fullmatch(finding.paragraph))
    return match[1], match[2], match[3] or ""


def _comments(inline: list[Finding], start: int) -> list[dict[str, str | int]]:
    comments: list[dict[str, str | int]] = []
    for number, finding in enumerate(inline, start):
        path, line = cast("tuple[str, int]", finding.location)
        _, prose, _ = _bullet(finding)
        if not _NUMBER.match(prose):
            prose = finding.visible
        comments.append(
            {
                "path": path,
                "line": line,
                "side": "RIGHT",
                "body": _NUMBER.sub(f"F{number}", prose, count=1),
            }
        )

    return comments


def _body_bullets(remaining: list[Finding], *, renumber: bool) -> dict[Finding, str]:
    replacements: dict[Finding, str] = {}
    for number, finding in enumerate(remaining, 1):
        if renumber or finding.location:
            bullet, prose, ending = _bullet(finding)
            if renumber:
                prose = _NUMBER.sub(f"F{number}", prose, count=1)
            if finding.location and not prose.rstrip().endswith(_UNANCHORED):
                prose = prose.rstrip() + _UNANCHORED
            replacements[finding] = f"{bullet}{prose}{ending}"
    return replacements


def render(
    body: str, found: list[Finding], diff: object
) -> tuple[str, list[dict[str, str | int]]]:
    """Render the body and comments in one sequence, with body findings first."""
    inline, remaining = split_findings(found, diff)
    start = len(remaining) + 1
    replacements = dict.fromkeys(inline, "")
    replacements.update(_body_bullets(remaining, renumber=bool(inline)))
    if inline:
        first = found[0]
        separate = "\n" if remaining else ""
        replacements[first] = (
            _count_line(inline, start) + separate + replacements[first]
        )
    for finding, replacement in sorted(
        replacements.items(), key=lambda pair: pair[0].offset, reverse=True
    ):
        offset = finding.offset
        end = offset + len(finding.paragraph)
        body = body[:offset] + replacement + body[end:]
    return body, _comments(inline, start)
