# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Payloads and temporary paths shared by the script guard tests."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.guard.scripts.paths import Place, normalize

if TYPE_CHECKING:
    from tbhbdev.alpha.repo import Json


PLACE = (
    "Write scripts for the operator to ~/.local/libexec/tbhb/"
    "YYYY-MM-DD-<owner>-<slug>.sh or .py. A revision is a new file with a .vN suffix, "
    "because a handed-over script is never edited. "
    "Run logs go in ~/.local/state/tbhb/<same name>/."
)


def isolate_repository_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the test's filesystem supplies repository markers.

    The temporary directory can itself sit under a repository or a harness
    marker. Neither belongs to the HOME and repositories the test constructs.
    """
    is_dir, is_file = Path.is_dir, Path.is_file

    def directory(path: Path) -> bool:
        if path.name == ".git" and not path.is_relative_to(tmp_path):
            return False
        return is_dir(path)

    def file(path: Path) -> bool:
        if path.name == ".git" and not path.is_relative_to(tmp_path):
            return False
        return is_file(path)

    monkeypatch.setattr(Path, "is_dir", directory)
    monkeypatch.setattr(Path, "is_file", file)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / "work").mkdir(parents=True)
    return path


@pytest.fixture
def libexec(home: Path) -> Path:
    path = home / ".local" / "libexec" / "tbhb"
    path.mkdir(parents=True)
    return path


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
