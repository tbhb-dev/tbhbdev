"""Tests for install files."""

import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.install import files, model

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def test_read_config(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    assert files.read_config(path) == (None, {})
    path.write_text('{"a": 1}\n')
    assert files.read_config(path) == ('{"a": 1}\n', {"a": 1})
    path.write_text("[]")
    with pytest.raises(model.InstallError, match=r"^it is not a JSON object$"):
        files.read_config(path)
    path.write_text("{")
    with pytest.raises(model.InstallError, match=r"^it is not valid JSON \("):
        files.read_config(path)


@pytest.mark.parametrize("text", ["", " \n\t\n"])
def test_read_config_empty_is_the_empty_object(tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(text)
    assert files.read_config(path) == (text, {})


def test_read_config_reports_a_file_it_cannot_read(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_bytes(b"\xff{}")
    with pytest.raises(
        model.InstallError,
        match=r"^it cannot be read \('utf-8' codec can't decode byte 0xff",
    ):
        files.read_config(path)
    path.unlink()
    path.mkdir()
    with pytest.raises(model.InstallError) as raised:
        files.read_config(path)
    assert (
        str(raised.value)
        == f"it cannot be read ([Errno 21] Is a directory: {str(path)!r})"
    )


def test_free_name(tmp_path: Path) -> None:
    path = tmp_path / "f"
    assert files.free_name(path) == path
    path.write_text("")
    (tmp_path / "f.1").symlink_to(tmp_path / "missing")
    assert files.free_name(path) == tmp_path / "f.2"


def test_stamp_is_utc_seconds() -> None:
    assert len(files.stamp()) == len("20261105T120000Z")
    assert files.stamp().endswith("Z")


@pytest.fixture
def tokyo(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Local time 9 hours ahead of UTC."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("tokyo")
def test_stamp_is_utc_in_another_time_zone() -> None:
    got = datetime.strptime(files.stamp(), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - got).total_seconds()) < 60


def test_write_config_new_file(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.json"
    assert files.write_config(path, None, "{}\n") is None
    assert path.read_text() == "{}\n"
    assert sorted(p.name for p in path.parent.iterdir()) == ["b.json"]


def test_write_config_backs_up_and_keeps_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "stamp", lambda: "T")
    path = tmp_path / "c.json"
    path.write_text("old\n")
    path.chmod(0o600)
    backup = files.write_config(path, "old\n", "new\n")
    assert backup == tmp_path / "c.json.tbhbdev-backup.T"
    assert backup is not None
    assert backup.read_text() == "old\n"
    assert path.read_text() == "new\n"
    assert path.stat().st_mode & 0o777 == 0o600
    again = files.write_config(path, "new\n", "newer\n")
    assert again == tmp_path / "c.json.tbhbdev-backup.T.1"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "xdg") == [
        "c.json",
        "c.json.tbhbdev-backup.T",
        "c.json.tbhbdev-backup.T.1",
    ]


def test_write_config_writes_through_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("old\n")
    link = tmp_path / "home" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(real)
    backup = files.write_config(link, "old\n", "new\n")
    assert link.is_symlink()
    assert real.read_text() == "new\n"
    assert backup is not None
    assert backup.parent == real.parent


def test_write_config_syncs_the_new_text_before_the_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "c.json"
    path.write_text("old\n")
    tmp = tmp_path / f".c.json.{os.getpid()}.tmp"
    synced: list[tuple[str, str]] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        synced.append((tmp.read_text(), path.read_text()))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    files.write_config(path, "old\n", "new\n")
    assert synced == [("new\n", "old\n")]
    assert path.read_text() == "new\n"
    assert not tmp.exists()


def test_write_config_removes_the_temporary_file_on_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "c.json"
    path.write_text("old\n")

    def fail(_fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(model.InstallError) as raised:
        files.write_config(path, "old\n", "new\n")
    assert str(raised.value) == "[Errno 5] Input/output error"
    assert path.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_write_config_reports_a_directory_it_cannot_make(tmp_path: Path) -> None:
    (tmp_path / "home").write_text("")
    path = tmp_path / "home" / ".claude" / "settings.json"
    with pytest.raises(model.InstallError) as raised:
        files.write_config(path, None, "{}\n")
    assert str(raised.value) == (f"[Errno 20] Not a directory: {str(path.parent)!r}")


def test_write_config_without_a_mode_keeps_the_old_mode_and_its_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "stamp", lambda: "T")
    path = tmp_path / "c.json"
    path.write_text("old\n")
    path.chmod(0o640)
    backup = files.write_config(path, "old\n", "new\n")
    assert backup is not None
    assert path.stat().st_mode & 0o777 == 0o640
    assert backup.stat().st_mode & 0o777 == 0o640
