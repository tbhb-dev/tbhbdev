# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Reads the tier 2 path map from the table in the root AGENTS.md."""

import re
import sys
from pathlib import Path

HEADING = "## Tier 2 paths"
GLOB_CELL = re.compile(r"\|\s*`([^`]+)`\s*\|")


def tier2_paths(text: str) -> str:
    """Return the table's globs in file order, one per line.

    The value is what `TIER2_PATHS` takes. A missing heading or an empty
    table raises, so an empty map can never read as "no tier 2".
    """
    lines = text.splitlines()
    if HEADING not in lines:
        message = f"AGENTS.md has no {HEADING!r} heading"
        raise ValueError(message)
    section: list[str] = []
    for line in lines[lines.index(HEADING) + 1 :]:
        if line.startswith("#"):
            break
        section.append(line)
    globs = [m.group(1) for line in section if (m := GLOB_CELL.match(line))]
    if not globs:
        message = f"the {HEADING!r} table has no rows"
        raise ValueError(message)
    return "\n".join(globs)


def main() -> None:
    (path,) = sys.argv[1:]
    print(tier2_paths(Path(path).read_text(encoding="utf-8")))  # noqa: T201


if __name__ == "__main__":
    main()
