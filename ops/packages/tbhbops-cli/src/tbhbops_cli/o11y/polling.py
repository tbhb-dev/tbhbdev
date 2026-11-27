"""Poll observability checks against a monotonic deadline."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from tbhbops_cli.cli import System

POLL_INTERVAL = 2


def poll(system: System, timeout: int, check: Callable[[], bool]) -> bool:
    deadline = system.monotonic() + timeout
    while True:
        if check():
            return True
        if system.monotonic() >= deadline:
            return False
        system.sleep(POLL_INTERVAL)
