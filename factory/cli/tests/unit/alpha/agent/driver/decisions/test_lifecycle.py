"""Test coordinator lifecycle decisions with literal values."""

from __future__ import annotations

import pytest

from tbhbdev.alpha.agent.driver.decisions import lifecycle
from tbhbdev.alpha.agent.model import Invalid


@pytest.mark.parametrize(
    ("rc", "launched", "expired", "result"),
    [
        (0, False, False, "finished"),
        (7, True, True, "finished"),
        (None, True, False, "launched"),
        (None, True, True, "launched"),
        (None, False, False, "poll"),
        (None, False, True, "deadline"),
    ],
)
def test_launch(rc: int | None, launched: bool, expired: bool, result: str) -> None:
    assert lifecycle.launch(rc, launched=launched, expired=expired) == result


@pytest.mark.parametrize(
    ("phase", "now", "action"),
    [
        ("workspace", 120, "deadline"),
        ("shims", 120, "poll"),
        ("shims", 719, "poll"),
        ("shims", 720, "deadline"),
    ],
)
def test_shim_launch_deadline(phase: str, now: float, action: str) -> None:
    deadline = lifecycle.launch_deadline(phase, 120, 600)
    assert lifecycle.launch(None, launched=False, expired=now >= deadline) == action


@pytest.mark.parametrize(
    ("rc", "session", "result"),
    [
        (0, True, "0"),
        (7, False, "7"),
        (None, True, "running"),
        (None, False, "unknown"),
    ],
)
def test_state(rc: int | None, session: bool, result: str) -> None:
    assert lifecycle.state(rc, session=session) == result


@pytest.mark.parametrize(
    ("rc", "session", "elapsed", "result"),
    [
        (0, True, 0, 0),
        (7, False, 239, 7),
        (None, True, 0, None),
        (None, False, 0, 1),
        (None, True, 240, 75),
        (None, True, 241, 75),
    ],
)
def test_wait(
    rc: int | None, session: bool, elapsed: float, result: int | None
) -> None:
    assert (
        lifecycle.wait(rc, session=session, failed_seconds=elapsed, unreachable=240)
        == result
    )


@pytest.mark.parametrize(
    ("rc", "session", "expired", "result"),
    [
        (0, True, False, "finished"),
        (7, False, True, "finished"),
        (None, False, False, "poll"),
        (None, True, False, "poll"),
        (None, False, True, "write"),
        (None, True, True, "refuse"),
    ],
)
def test_kill(rc: int | None, session: bool, expired: bool, result: str) -> None:
    assert lifecycle.kill(rc, session=session, expired=expired) == result


@pytest.mark.parametrize(
    ("rc", "session", "force", "message"),
    [
        (0, True, False, "run is running; clean refused"),
        (0, True, True, "run is running; clean refused"),
        (None, False, False, "run is unknown; clean requires --force"),
        (None, False, True, None),
        (
            0,
            False,
            False,
            (
                "full-access run may have unpublished commits or unread output; "
                "pass --force to clean anyway"
            ),
        ),
        (7, False, True, None),
    ],
)
def test_clean(rc: int | None, session: bool, force: bool, message: str | None) -> None:
    if message is None:
        assert lifecycle.clean(rc, session=session, force=force) is None
    else:
        with pytest.raises(Invalid) as caught:
            lifecycle.clean(rc, session=session, force=force)
        assert str(caught.value) == message


@pytest.mark.parametrize(("rc", "label"), [(None, "unknown"), (0, "0"), (7, "7")])
def test_answer_state(rc: int | None, label: str) -> None:
    assert lifecycle.answer_state(rc) == label
