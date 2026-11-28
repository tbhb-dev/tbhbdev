# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Temporary home and repository markers for script guard tests."""

from pathlib import Path

import pytest


@pytest.fixture
def _isolate_repository_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
def home(tmp_path: Path, _isolate_repository_markers: None) -> Path:
    path = tmp_path / "home"
    (path / "work").mkdir(parents=True)
    return path


@pytest.fixture
def libexec(home: Path) -> Path:
    path = home / ".local" / "libexec" / "tbhb"
    path.mkdir(parents=True)
    return path
