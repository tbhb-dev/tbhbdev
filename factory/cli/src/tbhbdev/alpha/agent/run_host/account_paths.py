"""Check path relationships for account configuration."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import PurePath


_STARTUP_FILES = (
    ".zshenv",
    ".zshrc",
    ".zprofile",
    ".zlogin",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".tmux.conf",
    ".config/tmux",
    ".gitconfig",
    ".config/git",
    ".config/mise",
    ".ssh",
    ".claude",
    ".claude.json",
    ".codex",
    ".codex-recovery",
)


def protected_home_paths[P: PurePath](home: P, sockets_dir: str) -> tuple[P, ...]:
    sockets = home / sockets_dir
    # HOME itself is the boundary: protecting it would refuse every workspace.
    return (
        *(home / p for p in _STARTUP_FILES),
        *(p for p in sockets.parents if p.is_relative_to(home) and p != home),
    )


def _overlaps(first: str, second: str) -> bool:
    one, two = PurePosixPath(first), PurePosixPath(second)
    return one == two or one in two.parents or two in one.parents


def protect_paths(writes: tuple[str, ...], protected: tuple[str, ...]) -> None:
    """Refuse either an ancestor or a descendant of a host trust path."""
    for path in protected:
        for write in writes:
            if _overlaps(path, write):
                message = (
                    f"protected host path {path} overlaps write root {write}; "
                    "narrow that write root"
                )
                raise Invalid(message)
