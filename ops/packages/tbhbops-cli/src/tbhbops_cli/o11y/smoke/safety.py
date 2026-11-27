"""Classify smoke failures and protect files from unsafe access."""

import stat
from typing import TYPE_CHECKING

from tbhbops.o11y import smoke

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbops_cli.o11y.filesystem import Filesystem


class SmokeError(Exception):
    """Only a fixed rule, never external text, may reach the transcript."""

    def __init__(self, rule: str, status: int = 1) -> None:
        super().__init__(rule)
        self.status = status


def require(passed: bool, rule: str) -> None:  # assert a classified result
    if not passed:
        raise SmokeError(rule)


def present[T](value: T | None, rule: str) -> T:
    if value is None:
        raise SmokeError(rule)
    return value


def safe_path(fs: Filesystem, path: Path) -> None:
    for part in (*reversed(path.parents), path):
        info = fs.lstat(part)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise SmokeError(smoke.FAILURES["usage_symlink_path_refused"], 2)


def private_directory(fs: Filesystem, path: Path) -> None:
    safe_path(fs, path)
    info = fs.lstat(path)
    if info is not None and (not stat.S_ISDIR(info.st_mode) or fs.list_dir(path)):
        raise SmokeError(smoke.FAILURES["usage_output_must_be_an_empty_directory"], 2)
    fs.prepare_output(path)
    fs.chmod(path, 0o700)


def scan_path(fs: Filesystem, path: Path, needles: tuple[bytes, ...]) -> None:
    """Refuse unreadable, missing, linked and vacuous search targets."""
    safe_path(fs, path)
    files = _scan_files(fs, path)
    require(any(files.values()), f"scan empty: {path.name}")
    for name, content in files.items():
        if content:
            rule = smoke.scan(content, needles)
            require(rule is None, f"{rule}: {name.name}")


def _scan_files(fs: Filesystem, path: Path) -> dict[Path, bytes]:
    info = present(fs.lstat(path), f"scan unavailable: {path.name}")
    if stat.S_ISDIR(info.st_mode):
        files = {}
        for name in fs.list_dir(path):
            files.update(_scan_files(fs, path / name))
        return files
    require(stat.S_ISREG(info.st_mode), f"scan unavailable: {path.name}")
    return {path: fs.read_bytes(path)}
