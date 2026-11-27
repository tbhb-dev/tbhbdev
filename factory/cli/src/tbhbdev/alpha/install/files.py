"""Read and atomically replace harness configuration files."""

import os
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tbhbdev.alpha.install.model import InstallError
from tbhbdev.alpha.install.settings import parse_config

if TYPE_CHECKING:
    from pathlib import Path
    from typing import IO

    from tbhbdev.alpha.install.model import Json


def read_text(path: Path) -> str | None:
    """A config's text, None when it is missing."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        message = f"it cannot be read ({exc})"
        raise InstallError(message) from exc


def read_config(path: Path) -> tuple[str | None, Json]:
    """Read a JSON config, keeping its original text for backups and diffs."""
    text = read_text(path)
    return text, parse_config(text)


def stamp() -> str:
    """The current UTC time, for backup names."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def free_name(path: Path) -> Path:
    """`path`, or `path.N` for the first N that names nothing yet."""
    found, n = path, 1
    while found.exists() or found.is_symlink():
        found = path.with_name(f"{path.name}.{n}")
        n += 1
    return found


def copy_backup(target: Path, backup: Path, mode: int | None) -> None:
    """Copy TARGET to BACKUP with its mode, or with MODE from the first byte."""
    if mode is None:
        shutil.copy2(target, backup)
        return
    backup.touch(mode=mode)
    shutil.copyfile(target, backup)


def open_tmp(tmp: Path, mode: int | None) -> IO[str]:
    """Open the temporary file for writing, created with MODE when given."""
    if mode is None:
        return tmp.open("w")
    return os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w")


def check_unchanged(target: Path, old: str | None) -> None:
    """Raise InstallError when TARGET no longer holds OLD (None: not checked).

    Text that isn't UTF-8 can't equal OLD, so it counts as a change.
    """
    try:
        changed = old is not None and target.read_text() != old
    except UnicodeDecodeError:
        changed = True
    if changed:
        message = f"{target} changed while it was being written"
        raise InstallError(message)


def write_config(
    path: Path, old: str | None, new: str, mode: int | None = None
) -> Path | None:
    """Replace a config atomically, backing up the old one; return the backup.

    The new text is synced to disk before it replaces the old. On any failure
    the temporary file goes, and the OSError becomes an InstallError. A MODE
    sets the permissions of the new file and the backup from creation on;
    without one, both keep the old file's. The file is read again just before
    the replace, and an edit since OLD was read aborts the write. An edit in the
    gap between that read and the replace is still lost; the backup keeps it.
    """
    target = path.resolve()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    backup = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if old is not None:
            name = f"{target.name}.tbhbdev-backup.{stamp()}"
            backup = free_name(target.with_name(name))
            copy_backup(target, backup, mode)
        with open_tmp(tmp, mode) as out:
            out.write(new)
            out.flush()
            os.fsync(out.fileno())
        if old is not None and mode is None:
            shutil.copymode(target, tmp)
        check_unchanged(target, old)
        tmp.replace(target)
    except (OSError, InstallError) as exc:
        # The temporary file may never have been made, or its directory may not
        # exist, so whatever unlink says is beside the point.
        with suppress(OSError):
            tmp.unlink()
        raise InstallError(str(exc)) from exc
    return backup
