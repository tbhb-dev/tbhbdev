"""Check how host executable lookup follows symbolic links."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tbhbdev import host_executables

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("relative", [False, True])
def test_executable_links_follow_directory_aliases_after_parent_components(
    tmp_path: Path, relative: bool
) -> None:
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    (release / "bin/codex").touch()
    directory = tmp_path / "bin"
    directory.mkdir()
    current = tmp_path / "current"
    current.symlink_to(
        release.relative_to(tmp_path) if relative else release, target_is_directory=True
    )
    alias = directory / "codex"
    alias.symlink_to("../current/bin/codex" if relative else current / "bin/codex")
    assert host_executables.executable_links(alias) == (str(alias), str(current))


def test_executable_link_walk_refuses_missing_paths(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        host_executables.executable_links(tmp_path / "missing")
