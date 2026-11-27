"""Resolve host executables using the configured search path."""

from __future__ import annotations

import shutil
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator


class ExecutableLookupError(Exception):
    """The executable is missing or is a mise shim."""


_SEARCH: ContextVar[str] = ContextVar(
    "host_search", default="/usr/bin:/bin:/usr/sbin:/sbin"
)


def search() -> str:
    return _SEARCH.get()


@contextmanager
def configured(path: str) -> Generator[None]:
    token = _SEARCH.set(path)
    try:
        yield
    finally:
        _SEARCH.reset(token)


def resolve(name: str, path: str | None = None, *, follow_symlinks: bool = True) -> str:
    directories = search() if path is None else path
    found = shutil.which(name, path=directories)
    if found is None:
        message = f"host executable {name} was not found on PATH {directories}"
        raise ExecutableLookupError(message)
    aliases = (Path(found), *(Path(p) for p in executable_links(Path(found))))
    if any(
        parent.name == "shims" and parent.parent.name == "mise"
        for alias in aliases
        for parent in (alias, alias.parent)
    ):
        message = (
            f"host executable {name} is a mise shim; "
            "configure its fixed install directory"
        )
        raise ExecutableLookupError(message)
    return str(Path(found).resolve() if follow_symlinks else Path(found).absolute())


def executable_links(path: Path) -> tuple[str, ...]:
    """Record directory and file links while walking an executable path."""
    return tuple(str(p) for p in executable_components(path) if p.is_symlink())


def executable_components(path: Path) -> Generator[Path]:
    """Walk every directory and link, including targets containing parent steps."""
    path.resolve(strict=True)
    pending = deque(path.parts)
    current = Path(pending.popleft())
    yield current
    while pending:
        part = pending.popleft()
        if part == "..":
            current = current.parent
            continue
        current /= part
        yield current
        if current.is_symlink():
            target = current.readlink()
            if target.is_absolute():
                current = Path(target.anchor)
                yield current
                pending.extendleft(reversed(target.parts[1:]))
            else:
                current = current.parent
                pending.extendleft(reversed(target.parts))
