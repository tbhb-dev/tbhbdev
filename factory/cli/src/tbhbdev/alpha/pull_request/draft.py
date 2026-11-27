# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Validate a draft and retain its title, text, and metadata."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator

from tbhbdev.alpha.pull_request import frontmatter
from tbhbdev.alpha.pull_request.body import (
    Line,
    Problem,
    Section,
    body_problems,
    heading_of,
    is_blank,
    ref_problems,
    scan,
)

DRAFT = Path(".github") / "PULL_REQUEST_DRAFT.md"


@dataclass(frozen=True)
class Checked:
    """Everything ``check`` learned. ``rendered`` runs from the title line on."""

    problems: list[Problem]
    title: str = ""
    rendered: list[str] = field(default_factory=list)
    meta: dict[str, object] = field(default_factory=dict)


if TYPE_CHECKING:
    import re
    from collections.abc import Sequence


@dataclass(frozen=True)
class DraftHead:
    """The parsed draft, beginning with the H1, and its findings so far."""

    title: str
    lines: list[Line]
    meta: dict[str, object]
    problems: list[Problem]


def check_head(
    text: str, data: object, body: str, schema: dict[str, object]
) -> DraftHead | Checked:
    """Validate frontmatter and locate the draft's title."""
    offset = text.count("\n") - body.count("\n")
    problems = frontmatter.meta_problems(
        data, offset, text, Draft202012Validator(schema)
    )
    meta = data if isinstance(data, dict) and not problems else {}
    lines, scanned = scan(body.splitlines(), offset + 1)
    problems += scanned
    start = next(
        (index for index, line in enumerate(lines) if not is_blank(line)), None
    )
    found = None if start is None else heading_of(lines[start])
    if start is None or found is None or found[0] != 1:
        where = offset + 1 if start is None else lines[start].no
        problems.append(
            (where, "the first line after the frontmatter must be the H1 title")
        )
        return Checked(sorted(problems))
    return DraftHead(found[1], lines[start:], meta, problems)


def check_body(
    head: DraftHead, sections: Sequence[Section], llm_line: re.Pattern[str]
) -> list[Problem]:
    """Validate the draft's body and references once its title is known."""
    return body_problems(
        head.lines[1:], head.lines[0], sections, llm_line
    ) + ref_problems(head.lines)


def checked_draft(head: DraftHead, problems: Sequence[Problem]) -> Checked:
    """Collect the findings from structure checks and the prose tools."""
    return Checked(
        sorted(set(head.problems + list(problems))),
        head.title,
        [line.raw for line in head.lines],
        head.meta,
    )
