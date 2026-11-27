"""Root-anchored descriptor walks for receiver writes and Compose path checks."""

import errno
import os
import re
import stat
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import cast
lazy from collections.abc import Generator

from tbhbops.deploy.protocol import RefusalError, require


def spelling(path: str) -> Path:
    require(re.fullmatch(r"/[A-Za-z0-9._/-]+", path) is not None, "path characters")
    require(all(part not in {".", ".."} for part in path.split("/")), "path components")
    return Path(path)


def directory(info: os.stat_result) -> None:
    require(stat.S_ISDIR(info.st_mode), "path component is not a directory")
    require(info.st_uid == 0, "directory owner")
    require(info.st_mode & 0o022 == 0, "writable directory")


@contextmanager
def parent(path: Path) -> Generator[tuple[int, str]]:
    """Every walk starts at /, including writes below the service root."""
    require(path.is_absolute() and path.name != "", "absolute file path required")
    spelling(str(path))
    fd = open_entry("/", directory=True)
    try:
        directory(os.fstat(fd))
        for part in path.parts[1:-1]:
            child = open_entry(part, directory=True, dir_fd=fd)
            os.close(fd)
            fd = child
            directory(os.fstat(fd))
        yield fd, path.name
    finally:
        os.close(fd)


def inspect(
    path: Path, *, file: bool = False, missing: bool = False
) -> os.stat_result | None:
    try:
        with parent(path) as (fd, name):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode), "symlink path")
            require(
                stat.S_ISREG(info.st_mode)
                if file
                else stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                "path type",
            )
            if file:
                require(info.st_mode & 0o022 == 0, "writable control file")
            return info
    except FileNotFoundError:
        require(missing, "missing path")
        return None


def read(path: Path, *, control: bool = False) -> bytes:
    with parent(path) as (fd, name):
        source = open_entry(name, dir_fd=fd)
        try:
            info = os.fstat(source)
            require(stat.S_ISREG(info.st_mode), "file type")
            if control:
                require(info.st_uid == 0, "control file owner")
                require(info.st_mode & 0o022 == 0, "writable control file")
            with os.fdopen(source, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(source)


def control_read(path: Path) -> bytes:
    """Inspect the path and validate the opened descriptor before reading bytes."""
    try:
        inspect(path, file=True)
        return read(path, control=True)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            message = "control path symlink or non-directory"
            raise RefusalError(message) from None
        raise


def write(
    path: Path, data: bytes, mode: int = 0o600, uid: int = 0, gid: int = 0
) -> None:
    with parent(path) as (fd, name):
        temporary = f".tmp-{uuid.uuid4().hex}"
        target = open_entry(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            dir_fd=fd,
        )
        try:
            with os.fdopen(target, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchown(stream.fileno(), uid, gid)
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
            # Refuse a symlink even though rename itself would replace the link.
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                require(stat.S_ISREG(info.st_mode), "secret target type")
            os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=fd)


def unlink(path: Path) -> None:
    with parent(path) as (fd, name):
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=fd)
        os.fsync(fd)


def mkdir(path: Path, mode: int = 0o700) -> None:
    with parent(path) as (fd, name):
        try:
            os.mkdir(name, mode=mode, dir_fd=fd)
        except FileExistsError:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            directory(info)
        else:
            os.chmod(name, mode, dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)


def pointer(path: Path, target: str | None) -> None:
    with parent(path) as (fd, name):
        if target is None:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=fd)
        else:
            temporary = f".pointer-{uuid.uuid4().hex}"
            os.symlink(target, temporary, dir_fd=fd)
            try:
                os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=fd)
        os.fsync(fd)


def release_name(value: str) -> str:
    match = re.fullmatch(r"releases/([0-9a-f]{40})", value)
    require(match is not None, "pointer target")
    return cast(
        # typing.cast ignores its type argument at runtime.
        "re.Match[str]",  # pragma: no mutate
        match,
    ).group(1)


def target(path: Path) -> str | None:
    with parent(path) as (fd, name):
        try:
            result = os.readlink(name, dir_fd=fd)
        except FileNotFoundError:
            return None
    release_name(result)
    return result


def sync(path: Path) -> None:
    with parent(path) as (fd, _):
        child = open_entry(path.name, directory=True, dir_fd=fd)
        try:
            os.fsync(child)
        finally:
            os.close(child)


def open_entry(
    name: str,
    flags: int = os.O_RDONLY,
    *,
    directory: bool = False,
    dir_fd: int | None = None,
) -> int:
    flags |= os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    return os.open(name, flags, 0o600, dir_fd=dir_fd)
