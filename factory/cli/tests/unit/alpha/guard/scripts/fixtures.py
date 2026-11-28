# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Payloads and temporary paths shared by the script guard tests."""

from typing import TYPE_CHECKING

from tbhbdev.alpha.guard.scripts.paths import Place, normalize

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.repo import Json


SHEBANG = "#!/bin/sh\n"
PLACE = (
    "Write scripts for the operator to ~/.local/libexec/tbhb/"
    "YYYY-MM-DD-<owner>-<slug>.sh or .py. A revision is a new file with a .vN suffix, "
    "because a handed-over script is never edited. "
    "Run logs go in ~/.local/state/tbhb/<same name>/."
)


def env_of(home: Path, tmpdir: str | None = None) -> dict[str, str]:
    return {"HOME": str(home)} | ({} if tmpdir is None else {"TMPDIR": tmpdir})


def bash(home: Path, command: str) -> Json:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(home / "work"),
    }


def tool(home: Path, name: str, **fields: object) -> Json:
    return {"tool_name": name, "tool_input": fields, "cwd": str(home / "work")}


def outside(path: object) -> str:
    return f"Scripts for the operator don't go in {path}. {PLACE}"


def places(home: Path, tmpdir: str = "") -> Place:
    return Place(str(home / "work"), normalize(str(home)), tmpdir)


def patch(*lines: str) -> str:
    return "\n".join(["*** Begin Patch", *lines, "*** End Patch"])
