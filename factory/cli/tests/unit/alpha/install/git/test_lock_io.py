"""Tests for install lock."""

import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev.alpha.install import model
from tbhbdev.alpha.install.git import lock as locking

if TYPE_CHECKING:
    from pathlib import Path


def dead_pid() -> int:
    done = subprocess.Popen([sys.executable, "-c", ""])
    done.wait()
    return done.pid


def test_dead_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_pid: int
) -> None:
    lock = tmp_path / "lock"
    lock.mkdir()
    assert locking.dead_owner(lock) is None
    (lock / "pid").write_text("x\n")
    assert locking.dead_owner(lock) is None
    (lock / "pid").write_text(f"{live_pid}\n")
    with monkeypatch.context() as probing:
        probe = Mock(wraps=os.kill)
        probing.setattr(os, "kill", probe)
        assert locking.dead_owner(lock) is None
        probe.assert_called_once_with(live_pid, 0)
    pid = dead_pid()
    (lock / "pid").write_text(f"{pid}\n")
    assert locking.dead_owner(lock) == pid

    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", denied)
    assert locking.dead_owner(lock) is None


def test_try_mkdir(tmp_path: Path) -> None:
    assert locking.try_mkdir(tmp_path / "d") is True
    assert locking.try_mkdir(tmp_path / "d") is False


def stale_lock(hooks: Path) -> Path:
    lock = hooks / locking.LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{dead_pid()}\n")
    return lock


def test_break_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], live_pid: int
) -> None:
    lock = tmp_path / locking.LOCK
    lock.mkdir()
    (lock / "pid").write_text(f"{live_pid}\n")
    assert locking.break_stale(lock) is False
    assert lock.is_dir()
    lock = stale_lock(tmp_path / "h")
    pid = (lock / "pid").read_text().strip()
    assert locking.break_stale(lock) is True
    assert not lock.exists()
    assert not lock.with_name(f"{lock.name}.break").exists()
    assert capsys.readouterr().err == (
        f"tbhbdev alpha install: removed stale lock {lock}; "
        f"process {pid} is not running\n"
    )


def test_break_stale_without_a_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's PID file can go between the check and the break."""
    lock = tmp_path / locking.LOCK
    lock.mkdir()

    def gone(_lock: Path) -> int:
        return 4242

    monkeypatch.setattr(locking, "dead_owner", gone)
    assert locking.break_stale(lock) is True
    assert not lock.exists()


def test_break_stale_leaves_a_lock_another_waiter_is_breaking(tmp_path: Path) -> None:
    lock = stale_lock(tmp_path)
    breaker = lock.with_name(f"{lock.name}.break")
    breaker.mkdir()
    assert locking.break_stale(lock) is False
    assert lock.is_dir()
    assert breaker.is_dir()


def test_break_stale_rechecks_the_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The owner was dead, but the lock was retaken before the break."""
    lock = stale_lock(tmp_path)
    answers = iter([1, None])

    def dead_owner(_lock: Path) -> int | None:
        return next(answers)

    monkeypatch.setattr(locking, "dead_owner", dead_owner)
    assert locking.break_stale(lock) is True
    assert (lock / "pid").is_file()
    assert capsys.readouterr().err == ""


def test_break_stale_keeps_a_lock_it_cannot_remove(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = stale_lock(tmp_path)
    (lock / "other").write_text("")
    assert locking.break_stale(lock) is True
    assert sorted(p.name for p in lock.iterdir()) == ["other"]
    assert capsys.readouterr().err == ""


def test_hooks_lock_takes_and_releases(tmp_path: Path) -> None:
    lock = tmp_path / locking.LOCK
    with locking.hooks_lock(tmp_path, 0):
        assert (lock / "pid").read_text() == f"{os.getpid()}\n"
    assert not lock.exists()


def test_hooks_lock_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_pid: int
) -> None:
    lock = tmp_path / locking.LOCK
    lock.mkdir()
    (lock / "pid").write_text(f"{live_pid}\n")
    naps: list[float] = []
    monkeypatch.setattr(time, "sleep", naps.append)
    with (
        pytest.raises(model.InstallError, match=r"within 2s; if no other install"),
        locking.hooks_lock(tmp_path, 2),
    ):
        pass
    assert naps == [0.1] * 20


def test_hooks_lock_waits_for_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / locking.LOCK
    lock.mkdir()

    def owner_finishes(_seconds: float) -> None:
        lock.rmdir()

    monkeypatch.setattr(time, "sleep", owner_finishes)
    with locking.hooks_lock(tmp_path, 1):
        assert lock.is_dir()


def test_hooks_lock_breaks_a_stale_lock(tmp_path: Path) -> None:
    stale_lock(tmp_path)
    with locking.hooks_lock(tmp_path, 0):
        pass
    assert not (tmp_path / locking.LOCK).exists()


def test_hooks_lock_warns_when_it_cannot_remove_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / locking.LOCK
    with locking.hooks_lock(tmp_path, 0):
        (lock / "other").write_text("")
    assert capsys.readouterr().err == (
        f"tbhbdev alpha install: warning: could not remove {lock}; remove it "
        "before the next run\n"
    )


def test_hooks_lock_serializes_threads(tmp_path: Path) -> None:
    inside: list[int] = []
    overlap: list[bool] = []

    def run() -> None:
        with locking.hooks_lock(tmp_path, 5):
            inside.append(1)
            overlap.append(len(inside) > 1)
            threading.Event().wait(0.05)
            inside.pop()

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap == [False, False, False]
