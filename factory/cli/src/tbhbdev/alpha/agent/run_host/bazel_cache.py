"""Identify and remove private Bazel output bases."""

from __future__ import annotations

import getpass
import hashlib
import os
import shutil
import stat
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import BazelShutdown, Prepared, Run


def _bazel_roots(home: Path) -> tuple[tuple[Path, Path], ...]:
    """The (trusted anchor, cache root) pairs of the agent's own Bazel caches."""
    user = f"_bazel_{getpass.getuser()}"
    return (
        (home, home / ".cache/bazel" / user),
        (Path("/var/tmp"), Path("/var/tmp") / user),  # noqa: S108  # nosec B108 -- the trusted /var/tmp anchor, not a temp file
    )


def bazel_bases(home: Path, worktree: str) -> tuple[Path, ...]:
    """Pin the output bases Bazel keys by the md5 of the workspace path.

    Prepare time computes them once, so a worktree swapped later cannot name
    another workspace's base. Only these default bases qualify for deletion.
    """
    names = {
        hashlib.md5(path.encode(), usedforsecurity=False).hexdigest()
        for path in (worktree, os.path.realpath(worktree))
    }
    return tuple(
        root / name for _, root in _bazel_roots(home) for name in sorted(names)
    )


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _owned_dir(status: os.stat_result) -> bool:
    return stat.S_ISDIR(status.st_mode) and status.st_uid == os.getuid()


def _open_owned(home: Path, base: Path) -> tuple[int, os.stat_result] | None:
    """Open the base's parent and return its fd with the base's status.

    Every directory below the trusted anchor is opened with O_NOFOLLOW relative
    to the one before, and must be owned by the agent's uid, so a symlinked
    ancestor cannot redirect a deletion and the returned fd pins the validated
    chain even if a path is replaced later. The caller closes the fd.
    """
    anchor = next((a for a, root in _bazel_roots(home) if base.parent == root), None)
    if anchor is None or ".." in base.parts:
        return None
    names = base.relative_to(anchor).parts
    try:
        fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for name in names[:-1]:
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            if not _owned_dir(os.fstat(fd)):
                raise NotADirectoryError(name)
        status = os.stat(names[-1], dir_fd=fd, follow_symlinks=False)
        if not _owned_dir(status):
            raise NotADirectoryError(names[-1])
    except OSError:
        os.close(fd)
        return None
    return fd, status


def owned_base(home: Path, base: Path) -> os.stat_result | None:
    """Return the base's lstat when it is a real directory the agent owns."""
    opened = _open_owned(home, base)
    if opened is None:
        return None
    os.close(opened[0])
    return opened[1]


def server_alive(base: Path) -> bool:
    try:
        pid = int((base / "server/server.pid.txt").read_text())
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except PermissionError:
        # A live process of another uid, or an unreadable pid file: keep the base.
        return True
    except OSError, ValueError, OverflowError:
        # No readable pid file, or no such process, means no server we can see.
        return False
    return True


def _remove_base(parent_fd: int, name: str) -> None:
    """Delete a base relative to its parent's fd, unlocking only inside it.

    shutil.rmtree opens each directory through dir_fd where the platform allows,
    so it does not follow a symlink swapped in during the walk. The unlock chmod
    goes through the same fd and only touches directories the agent owns.
    """

    def unlock(_function: object, path: str, _error: BaseException) -> None:
        for target in {path, str(Path(path).parent)} - {"."}:
            with suppress(OSError):
                if _owned_dir(os.stat(target, dir_fd=parent_fd, follow_symlinks=False)):
                    os.chmod(target, 0o700, dir_fd=parent_fd)

    for _attempt in range(3):
        shutil.rmtree(name, onexc=unlock, dir_fd=parent_fd)
        if not _exists(parent_fd, name):
            return


def _exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _space(fd: int) -> tuple[int, int]:
    status = os.fstatvfs(fd)
    return status.f_bavail * status.f_frsize, status.f_favail


def _delete_base(
    run: Run, parent_fd: int, name: str, identity: tuple[int, int]
) -> tuple[int, int] | None:
    """Delete a base, returning the (bytes, inodes) freed, or None on failure.

    Re-checks the base's identity through the pinned parent fd just before removal.
    """
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise FileNotFoundError(name)
        before = _space(parent_fd)
        _remove_base(parent_fd, name)
        after = _space(parent_fd)
    except OSError:
        run.files.write("log", "bazel cleanup failed\n")
        return None
    if _exists(parent_fd, name):
        run.files.write("log", "bazel cleanup failed\n")
        return None
    return max(0, after[0] - before[0]), max(0, after[1] - before[1])


def _unlink_bazel_links(worktree: Path, deleted: list[Path]) -> None:
    for link in (*worktree.glob("bazel-*"), *worktree.glob(".bazel-*")):
        if not link.is_symlink():
            continue
        target = Path(os.path.normpath(link.parent / link.readlink()))
        if any(target.is_relative_to(base) for base in deleted):
            with suppress(OSError):
                link.unlink()


def _clean_base(
    run: Run, base: Path, result: str, identity: tuple[int, int]
) -> tuple[int, int] | None:
    """Delete one base unless it changed, its shutdown failed, or a server holds it."""
    opened = _open_owned(run.home, base)
    if opened is None:
        run.files.write("log", f"bazel cleanup: kept {base} ({result})\n")
        return None
    fd, status = opened
    try:
        if result != "rc=0" or (status.st_dev, status.st_ino) != identity:
            reason = result
        elif server_alive(base):
            reason = "server alive"
        else:
            return _delete_base(run, fd, base.name, identity)
        run.files.write("log", f"bazel cleanup: kept {base} ({reason})\n")
        return None
    finally:
        os.close(fd)


def cleanup_bazel(
    run: Run,
    prepared: Prepared | None,
    stopped: BazelShutdown,
) -> dict[str, int]:
    """Delete the recorded output bases whose server is gone, and the links into them.

    A base is deleted only when its shutdown returned 0 and its server pid is
    dead at that moment, it is still the same directory (st_dev, st_ino) as at
    shutdown, and it is owned by the agent with no symlinked ancestor. The
    deletion runs relative to an fd that pins the validated ancestors. The freed
    counts are the statvfs delta on the base's filesystem across the deletion, so
    they include any other writer on that host, and a failed deletion adds nothing.
    """
    freed = {"bytes": 0, "inodes": 0, "bases": 0}
    if prepared is None:
        return freed
    deleted: list[Path] = []
    for base, (result, identity) in stopped.items():
        if identity is None:
            continue
        space = _clean_base(run, base, result, identity)
        if space is None:
            continue
        freed["bytes"] += space[0]
        freed["inodes"] += space[1]
        freed["bases"] += 1
        deleted.append(base)
    worktree = Path(prepared.paths.worktree)
    if deleted and not worktree.is_symlink():
        _unlink_bazel_links(worktree, deleted)
    if freed["bases"]:
        run.files.write("log", f"bazel cleanup: freed {freed}\n")
    return freed
