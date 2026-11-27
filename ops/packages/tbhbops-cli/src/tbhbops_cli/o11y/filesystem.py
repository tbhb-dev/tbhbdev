"""Host filesystem operations for observability preparation."""

import os
import stat
import tempfile
from contextlib import contextmanager, suppress
from typing import Protocol
lazy from collections.abc import Generator
lazy from pathlib import Path


class Filesystem(Protocol):
    def temporary_directory(self) -> Path: ...
    def prepare_output(self, path: Path) -> Path: ...
    def lstat(self, path: Path) -> os.stat_result | None: ...
    def socket_lstat(self, path: Path) -> os.stat_result | None: ...
    def read_text(self, path: Path) -> str: ...
    def read_bytes(self, path: Path) -> bytes: ...
    def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None: ...
    def mkdir(self, path: Path) -> None: ...
    def chown(self, path: Path, uid: int, gid: int) -> None: ...
    def chmod(self, path: Path, mode: int) -> None: ...
    def list_dir(self, path: Path) -> tuple[str, ...]: ...
    def unlink(self, path: Path) -> None: ...
    def rmdir(self, path: Path) -> None: ...
    def rename(self, source: Path, target: Path) -> None: ...


def _open(name: str, flags: int, *, dir_fd: int | None = None) -> int:
    """Open an entry without following links on supported Unix hosts."""
    flags |= os.O_NOFOLLOW
    return os.open(
        name,
        flags,
        0o600,  # pragma: no mutate  # umask and fchmod also fix access
        dir_fd=dir_fd,
    )


@contextmanager
def _parent(
    path: Path, root: Path | None = None, *, read_directory: bool = False
) -> Generator[tuple[int, str]]:
    """Open from the trusted render root, or the anchor for init and source reads."""
    absolute = path.absolute()
    # O_PATH needs only search permission on Linux. O_DIRECTORY with O_NOFOLLOW
    # still rejects symlinks at every component, even though O_PATH alone could
    # open a symlink itself. Atomic writes need a readable fd for directory fsync.
    flags = os.O_RDONLY if read_directory else getattr(os, "O_PATH", os.O_RDONLY)
    flags |= os.O_DIRECTORY
    start = (
        root if root is not None and root in absolute.parents else Path(absolute.anchor)
    )
    fd = _open(str(start), flags)
    try:
        for part in absolute.relative_to(start).parts[:-1]:
            child = _open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, absolute.relative_to(start).name or "."
    finally:
        os.close(fd)


class LocalFilesystem:
    def __init__(self) -> None:
        self.root: Path | None = None

    def temporary_directory(self) -> Path:
        return Path(os.path.realpath(tempfile.mkdtemp(prefix="o11y-validate-")))

    def prepare_output(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = Path(os.path.realpath(path))
        return self.root

    def lstat(self, path: Path) -> os.stat_result | None:
        try:
            with _parent(path, self.root) as (fd, name):
                return os.stat(
                    name,
                    dir_fd=fd,
                    follow_symlinks=False,  # pragma: no mutate  # None is also false
                )
        except FileNotFoundError:
            return None

    def socket_lstat(self, path: Path) -> os.stat_result | None:
        return self.lstat(Path(os.path.realpath(path.parent)) / path.name)

    def read_text(self, path: Path) -> str:
        with _parent(path, self.root) as (fd, name):
            source = _open(name, os.O_RDONLY, dir_fd=fd)
            with os.fdopen(source, newline="") as stream:
                return stream.read()

    def read_bytes(self, path: Path) -> bytes:
        with _parent(path, self.root) as (fd, name):
            source = _open(name, os.O_RDONLY, dir_fd=fd)
            with os.fdopen(source, "rb") as stream:
                return stream.read()

    def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None:
        with _parent(path, self.root, read_directory=True) as (fd, name):
            temporary = f".tmp.{name}"
            mask = os.umask(0o077)  # pragma: no mutate  # 0600 also limits access
            try:
                target = _open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=fd
                )
            finally:
                os.umask(mask)
            try:
                content = (
                    text.encode(errors="surrogateescape")
                    if isinstance(text, str)
                    else text
                )
                with os.fdopen(target, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            except BaseException:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=fd)
                raise
            os.fsync(fd)

    def mkdir(self, path: Path) -> None:
        info = self.lstat(path)
        if info is None:
            with _parent(path, self.root) as (fd, name):
                os.mkdir(name, mode=0o700, dir_fd=fd)
        elif not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError

    def chown(self, path: Path, uid: int, gid: int) -> None:
        with _parent(path, self.root) as (fd, name):
            target = _open(name, os.O_RDONLY, dir_fd=fd)
            try:
                os.fchown(target, uid, gid)
            finally:
                os.close(target)

    def chmod(self, path: Path, mode: int) -> None:
        with _parent(path, self.root) as (fd, name):
            target = _open(name, os.O_RDONLY, dir_fd=fd)
            try:
                os.fchmod(target, mode)
            finally:
                os.close(target)

    def list_dir(self, path: Path) -> tuple[str, ...]:
        with _parent(path, self.root) as (fd, name):
            child = _open(name, os.O_RDONLY, dir_fd=fd)
            try:
                return tuple(
                    sorted(os.listdir(child))
                )  # directory fd prevents symlink traversal
            finally:
                os.close(child)

    def unlink(self, path: Path) -> None:
        with _parent(path, self.root) as (fd, name):
            os.unlink(name, dir_fd=fd)

    def rmdir(self, path: Path) -> None:
        with _parent(path, self.root) as (fd, name):
            os.rmdir(name, dir_fd=fd)

    def rename(self, source: Path, target: Path) -> None:
        with (
            _parent(source, self.root) as (src_fd, src_name),
            _parent(target, self.root) as (dst_fd, dst_name),
        ):
            os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)


def effective_uid() -> int:
    """The Unix effective uid used for init."""
    return os.geteuid()


def effective_gid() -> int:
    """The Unix effective group used for private probe preparation."""
    return os.getegid()
