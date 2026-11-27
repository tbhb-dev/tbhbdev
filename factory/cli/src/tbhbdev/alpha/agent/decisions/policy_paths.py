"""Resolve run paths and enforce filesystem policy boundaries."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid, Os, Profile

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import RunPaths, Settings

# Process substitution and /dev/stderr need access to open descriptors on macOS.
DEV_FD = "/dev/fd"
# macOS's `/tmp` is a link to `/private/tmp`, so a denied path under it needs both.
_TMP = "/tmp"  # noqa: S108 # nosec B108 -- an alias, not a temp file
GIT_CONFIG_PATHS = (
    ".git",
    ".git/config",
    ".git/config.worktree",
    ".git/commondir",
    ".git/hooks",
    ".git/info",
    ".git/modules",
)


def resolve(home: str, path: str) -> str:
    """PATH as an absolute path, resolved against HOME unless it's absolute."""
    return str(PurePosixPath(home) / path)


def _with_aliases(os: Os, path: str) -> tuple[str, ...]:
    """PATH, and on macOS its `/private` twin when it's under `/tmp`."""
    if os is Os.MACOS and (path == _TMP or path.startswith(f"{_TMP}/")):
        return (path, f"/private{path}")
    return (path,)


def denied_paths(
    settings: Settings, os: Os, home: str, uid: int, token_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Collect denied paths, add macOS aliases and drop covered descendants."""
    paths = [resolve(home, path) for path in token_paths]
    if os is Os.LINUX:
        paths.extend(
            ("/run/WSL", "/var/run/docker.sock", "/run/docker.sock", f"/run/user/{uid}")
        )
    paths.append(resolve(home, settings.sockets_dir))
    paths.extend(resolve(home, p) for p in settings.sandbox.denied_home_paths)
    paths.extend(c.signing for c in settings.credentials if c.signing.startswith("/"))
    unique = tuple(
        dict.fromkeys(alias for path in paths for alias in _with_aliases(os, path))
    )
    # A child under a denied parent is already hidden. Masking it as well makes
    # bwrap mkdir its mount point inside the parent's read-only mask and fail.
    # This compares path strings without following symlinks.
    return tuple(
        path
        for path in unique
        if not any(str(parent) in unique for parent in PurePosixPath(path).parents)
    )


def write_roots(
    settings: Settings, profile: Profile, paths: RunPaths
) -> tuple[str, ...]:
    if profile is Profile.REVIEW:
        return tuple(dict.fromkeys((paths.tmpdir, paths.audit_dir)))
    roots = (resolve(paths.home, r) for r in settings.sandbox.write_roots)
    return tuple(dict.fromkeys((paths.worktree, paths.tmpdir, *roots)))


def protected_git(paths: RunPaths) -> tuple[str, ...]:
    return tuple(resolve(paths.worktree, entry) for entry in GIT_CONFIG_PATHS)


def _overlaps(first: str, second: str) -> bool:
    one, two = PurePosixPath(first), PurePosixPath(second)
    return one == two or one in two.parents or two in one.parents


def refuse_overlaps(paths: tuple[str, ...], deny: tuple[str, ...], kind: str) -> None:
    for path in paths:
        for denied in deny:
            if _overlaps(path, denied):
                message = f"policy {kind} {path} overlaps the denied path {denied}"
                raise Invalid(message)


def protect_host(
    settings: Settings, paths: RunPaths, protected: tuple[str, ...]
) -> None:
    """Refuse host trust paths on either side of a writable path."""
    writes = (
        paths.worktree,
        paths.tmpdir,
        paths.audit_dir,
        DEV_FD,
        *(resolve(paths.home, p) for p in settings.sandbox.write_roots),
        *(resolve(paths.home, p) for p in settings.sandbox.agy.build_write),
    )
    protect_paths(writes, protected)
    protect_paths(writes[1:], (resolve(paths.worktree, ".git/hooks"),))


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
