# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read and check draft files."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


from tbhbdev.alpha.identity.commit.grammar import LINE
from tbhbdev.alpha.pull_request import baseline, frontmatter, prose
from tbhbdev.alpha.pull_request.draft import (
    Checked,
    check_body,
    check_head,
    checked_draft,
)


def check_text(text: str) -> Checked:
    """Validate and lint a draft. The linters run only once there is a title."""
    try:
        data, body = frontmatter.parse_frontmatter(text)
    except frontmatter.FrontmatterError as exc:
        return Checked([(1, f"frontmatter: {exc}")])
    head = check_head(text, data, body, baseline.load_schema())
    if isinstance(head, Checked):
        return head
    first = head.lines[0].no
    problems = [(first, why) for why in prose.title_problems(head.title)]
    problems += check_body(head, baseline.load_sections(), LINE)
    problems += prose.lint([line.raw for line in head.lines], first)
    return checked_draft(head, problems)


def load_checked(path: Path) -> Checked:
    """Check a draft file."""
    try:
        text = path.read_text()
    except OSError as exc:
        return Checked([(1, f"cannot read the draft: {exc.strerror}")])
    return check_text(text)


def read_checked(path: Path) -> Checked:
    """Check a draft file and print its problems."""
    checked = load_checked(path)
    for line, why in checked.problems:
        print(f"{path}:{line}: {why}")
    return checked
