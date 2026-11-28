"""Resolve host commands independently of the caller's search path."""

from __future__ import annotations

import base64
import shutil
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

from tbhbdev.alpha import tool_pins
from tbhbdev.alpha.agent.model import Invalid, Os, Settings

_SEARCH: ContextVar[str] = ContextVar(
    "host_search", default="/usr/bin:/bin:/usr/sbin:/sbin"
)


def search_path(settings: Settings, os: Os) -> tuple[str, ...]:
    entries = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    if os is Os.MACOS:
        entries.extend(("/opt/homebrew/bin", "/usr/local/bin"))
    return (*entries, *settings.sandbox.host_path)


def account_path(home: str) -> tuple[str, ...]:
    """Search the run account's tool directories without sandbox settings."""
    return (
        f"{home}/.local/bin",
        f"{home}/.local/gh-bin",
        f"{home}/.bun/bin",
        f"{home}/.npm-global/bin",
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    )


def require_check_tools(workspace: Path, env: Mapping[str, str] | None) -> None:
    if env is None:
        return
    try:
        _, problems = tool_pins.check_tools(workspace, env, Path.home())
    except (OSError, TypeError, ValueError) as exc:
        message = "could not read the check tool pins from mise.toml"
        raise Invalid(message) from exc
    if problems:
        details = ", ".join(problems.values())
        message = (
            f"pinned check tools: {details}. Run `tbhbdev alpha install` "
            "from a checkout at the run's starting ref."
        )
        raise Invalid(message)


def search() -> str:
    return _SEARCH.get()


def git_token_env(token: str | None) -> dict[str, str]:
    """Pass an HTTP token to Git without exposing it in the argument list."""
    if token is None:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


@contextmanager
def configured(settings: Settings, os: Os) -> Generator[None]:
    token = _SEARCH.set(":".join(search_path(settings, os)))
    try:
        yield
    finally:
        _SEARCH.reset(token)


def resolve(name: str, path: str | None = None, *, follow_symlinks: bool = True) -> str:
    directories = search() if path is None else path
    found = shutil.which(name, path=directories)
    if found is None:
        message = f"host executable {name} was not found on PATH {directories}"
        raise Invalid(message)
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
        raise Invalid(message)
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
