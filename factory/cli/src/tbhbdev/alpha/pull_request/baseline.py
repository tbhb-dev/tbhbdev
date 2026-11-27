# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Load the draft schema and section scheme from the worktree."""

import json
from pathlib import Path
from typing import cast

from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.body import Section, parse_sections

# Names live at module level, where mutmut leaves them alone.
SCHEMA = Path(".github") / "schemas" / "pull-request-draft.schema.json"
SECTIONS = Path(".github") / "pull-request-sections.toml"


def read_baseline(path: Path) -> str:
    """Read one of the shared baseline files from the worktree.

    Raises:
        SystemExit: The file is missing.
    """
    try:
        return (pr_tools.root() / path).read_text()
    except FileNotFoundError:
        message = (
            f"{path} is missing: this repository needs the shared baseline"
            " from tbhb-dev/rules"
        )
        raise SystemExit(message) from None


def load_sections() -> list[Section]:
    """Read the section scheme."""
    return parse_sections(read_baseline(SECTIONS))


def load_schema() -> dict[str, object]:
    """Read the frontmatter schema."""
    text = read_baseline(SCHEMA)
    return cast("dict[str, object]", json.loads(text))  # pragma: no mutate
