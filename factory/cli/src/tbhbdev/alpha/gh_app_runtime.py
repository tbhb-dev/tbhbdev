"""Environment and operating system dependencies for GitHub App tokens."""

import os
import subprocess  # nosec B404
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from tbhbdev import secrets
lazy from tbhbdev.alpha.agent import host_tools
lazy from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx2

RUN_ENV = "TBHBDEV_AGENT_RUN"  # Set by exec in every run's environment.
# ruff wants `check` written out; False is subprocess.run's default.
NO_CHECK = False


class GhAppError(Exception):
    """A mint failed. The message never holds a token, a key or a JWT."""


class Reader(Protocol):
    def read(self, ref: str, /) -> str: ...


class OpenSsl(Protocol):
    def __call__(
        self, argv: list[str], data: bytes | None = None, /
    ) -> subprocess.CompletedProcess[bytes]: ...


@dataclass(frozen=True)
class Env:
    state: Path
    now: Callable[[], int]
    store: Callable[[], Reader]
    openssl: OpenSsl
    transport: httpx2.BaseTransport | None = None


def state_root(environ: dict[str, str], home: Path) -> Path:
    base = environ.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / "gh-app"


def run_openssl(
    argv: list[str], data: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # nosec B603 B607
            [host_tools.resolve("openssl"), *argv],
            input=data,
            capture_output=True,
            check=NO_CHECK,
        )
    except FileNotFoundError, Invalid:
        message = "openssl isn't installed"
        raise GhAppError(message) from None


def clock() -> int:
    return int(time.time())


def default_env() -> Env:
    return Env(
        state_root(dict(os.environ), Path.home()),
        clock,
        secrets.Store,
        run_openssl,
    )


def fill(handle: int, text: str) -> None:
    with os.fdopen(handle, "wb") as stream:
        stream.write(text.encode())
        stream.flush()
        os.fsync(stream.fileno())
