"""Audit writable roots for overlapping Git checkouts."""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from pathlib import Path

CHECKOUT_SCAN_DEPTH = 3
COMMON_DIRECTORY_LIMIT = 4096


def _own_common_directory(git_dir: Path) -> bool:
    common = git_dir / "commondir"
    try:
        metadata = common.lstat()
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(metadata.st_mode):
        message = f"{common} is not a regular file"
        raise OSError(message)
    # Do not follow a replacement link or block on a replacement FIFO.
    fd = os.open(common, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            message = f"{common} is not a regular file"
            raise OSError(message)
        content = stream.read(COMMON_DIRECTORY_LIMIT + 1)
    if len(content) > COMMON_DIRECTORY_LIMIT:
        message = f"{common} is longer than {COMMON_DIRECTORY_LIMIT} bytes"
        raise OSError(message)
    target = content.decode("utf-8").strip()
    return (git_dir / target).resolve() == git_dir.resolve()


def _is_git_directory(git_dir: Path) -> bool:
    try:
        return stat.S_ISDIR(git_dir.lstat().st_mode)
    except FileNotFoundError, NotADirectoryError:
        return False


def is_main_checkout(directory: Path, findings: list[str] | None = None) -> bool:
    """Whether DIRECTORY has a regular non-empty HEAD and no foreign commondir.

    Sandbox placeholders are not checkouts. Unreadable or non-regular Git
    metadata produces an audit finding instead of interrupting the launch.
    """
    git_dir = directory / ".git"
    head = git_dir / "HEAD"
    try:
        if not _is_git_directory(git_dir):
            return False
        try:
            metadata = head.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            if findings is not None:
                findings.append(
                    f"cannot audit main checkout {directory}: "
                    f"{head} is not a regular file"
                )
            return False
        return metadata.st_size > 0 and _own_common_directory(git_dir)
    except (OSError, UnicodeError, ValueError) as exc:
        if findings is not None:
            findings.append(f"cannot audit main checkout {directory}: {exc}")
        return False


def _contained_checkouts(root: Path, workspace: Path | None) -> list[str]:
    found: list[str] = []

    def unreadable(error: OSError) -> None:
        # A vanished path or a closed descriptor (under /dev/fd) hides nothing.
        benign = isinstance(error, FileNotFoundError | NotADirectoryError)
        if not benign and error.errno != errno.EBADF:
            found.append(
                f"cannot audit write root {root}: cannot read {error.filename}: {error}"
            )

    for directory, children, _files in root.walk(on_error=unreadable):
        if directory != workspace and is_main_checkout(directory, found):
            found.append(f"write root {root} contains main checkout {directory}")
        if len(directory.relative_to(root).parts) >= CHECKOUT_SCAN_DEPTH:
            children.clear()
        if ".git" in children:
            children.remove(".git")
    return found


SANDBOX_MOUNT_POINTS = (".git", ".agents", ".aws", ".codex")


def clear_mount_points(writes: tuple[str, ...]) -> None:
    """Remove the empty directories a sandbox leaves in a write root.

    The sandbox creates these names as protected mount points. An empty `.git`
    would make the next launch refuse the root as a main checkout. `rmdir`
    leaves any directory that holds something, and a symlink is never followed.
    """
    for root in writes:
        for name in SANDBOX_MOUNT_POINTS:
            entry = Path(root) / name
            if entry.is_dir(follow_symlinks=False):
                with contextlib.suppress(OSError):
                    entry.rmdir()


def validate_writes(
    workspace: Path, source: Path, writes: tuple[str, ...]
) -> tuple[str, ...]:
    """Report the write roots that overlap a main checkout, and refuse none.

    The decision of 2026-11-12 makes the directory write allowlist audit-only:
    the launcher records what it would have refused and starts the run.
    """
    clear_mount_points(writes)
    workspace, source = workspace.resolve(), source.resolve()
    own = workspace if workspace.parent == source / ".worktrees" else None
    found: list[str] = []
    for root in (workspace, *(Path(p).resolve() for p in writes)):
        for ancestor in (root, *root.parents):
            if ancestor == own:
                break
            if is_main_checkout(ancestor, found):
                found.append(f"write root {root} reaches main checkout {ancestor}")
                break
        if source.is_relative_to(root):
            found.append(f"write root {root} contains main checkout {source}")
        found.extend(_contained_checkouts(root, own))
    return tuple(found)
