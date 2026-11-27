"""Serialize concurrent Git hook installs with a PID lock."""

import os
import re
import sys
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

from tbhbdev.alpha.install.model import PROG, InstallError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


LOCK = ".tbhb-install.lock"
LOCK_WAIT = 15
PID = "pid"
PID_TEXT = re.compile(r"[0-9]+")


def dead_owner(lock: Path) -> int | None:
    """The PID in the lock's pid file when that process isn't running.

    A lock without a readable PID (a run killed between taking the lock and
    writing the file) counts as held, so it waits out the timeout.
    """
    try:
        raw = (lock / PID).read_text().strip()
    except OSError:
        return None
    if not PID_TEXT.fullmatch(raw):
        return None
    pid = int(raw)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid
    except PermissionError:
        return None
    return None


def try_mkdir(path: Path) -> bool:
    """Make a directory; False when it already exists. mkdir is atomic."""
    try:
        path.mkdir()
    except FileExistsError:
        return False
    return True


def break_stale(lock: Path) -> bool:
    """Remove the lock if its owner is gone; whether this run tried.

    Only the run holding `<lock>.break` may break the lock, and it checks the
    owner again first, so two waiters can't both break it and remove the lock
    the other has just taken.
    """
    breaker = lock.with_name(f"{lock.name}.break")
    if dead_owner(lock) is None or not try_mkdir(breaker):
        return False
    pid = dead_owner(lock)
    if pid is not None:
        (lock / PID).unlink(missing_ok=True)
        with suppress(OSError):
            lock.rmdir()
            print(
                f"{PROG}: removed stale lock {lock}; process {pid} is not running",
                file=sys.stderr,
            )
    breaker.rmdir()
    return True


@contextmanager
def hooks_lock(hooks: Path, timeout: int) -> Generator[None]:
    """Hold the hooks directory's install lock."""
    lock = hooks / LOCK
    tenths = 0
    while not try_mkdir(lock):
        if break_stale(lock):
            continue
        if tenths >= timeout * 10:
            message = (
                f"could not take {lock} within {timeout}s; if no other install "
                "is running, remove it"
            )
            raise InstallError(message)
        time.sleep(0.1)
        tenths += 1
    try:
        (lock / PID).write_text(f"{os.getpid()}\n")
        yield
    finally:
        (lock / PID).unlink(missing_ok=True)
        try:
            lock.rmdir()
        except OSError:
            print(
                f"{PROG}: warning: could not remove {lock}; remove it before the "
                "next run",
                file=sys.stderr,
            )
