"""Smoke polling deadlines and intervals."""

from tbhbops_cli.o11y import polling
from tbhbops_cli.o11y.smoke import (
    bootstrap,
    readiness,
)
from unit.o11y.smoke.runtime_fixture import make_system


def test_poll_timing() -> None:
    _, _, system = make_system()
    calls = []

    def check() -> bool:
        calls.append(system.monotonic())
        return False

    assert not polling.poll(system, 30, check)
    assert calls == list(range(0, 31, 2))
    assert polling.poll(system, 180, lambda: True)
    assert (
        readiness.READY_TIMEOUT,
        readiness.HEALTH_TIMEOUT,
        readiness.FIXTURE_TIMEOUT,
        polling.POLL_INTERVAL,
        readiness.WAIT_MARGIN,
        bootstrap.PULL_TIMEOUT,
    ) == (180, 120, 30, 2, 30, 600)
