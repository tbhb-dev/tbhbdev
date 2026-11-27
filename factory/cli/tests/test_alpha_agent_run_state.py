"""Lifecycle tables use literal file contents, identities, kinds and times."""

from __future__ import annotations

import pytest

from tbhbdev.alpha.agent.decisions import run_state as state


@pytest.mark.parametrize(
    ("timeout", "maximum", "wanted"),
    [
        (None, None, None),
        (13, None, 13),
        (None, 2, 120),
        (121, 2, 120),
        (119, 2, 119),
    ],
)
def test_timeout(timeout: int | None, maximum: int | None, wanted: int | None) -> None:
    assert state.time_limit(timeout, maximum) == wanted


@pytest.mark.parametrize(
    ("pgid", "current", "wanted"),
    [
        (23, state.Group(23, "boot-a", "42"), True),
        (23, None, False),
        (23, state.Group(24, "boot-a", "42"), False),
        (23, state.Group(23, "boot-b", "42"), False),
        (23, state.Group(23, "boot-a", "43"), False),
        (2, state.Group(2, "boot-a", "42"), True),
        (1, state.Group(1, "boot-a", "42"), False),
        (0, state.Group(0, "boot-a", "42"), False),
    ],
)
def test_ownership(pgid: int, current: state.Group | None, wanted: bool) -> None:
    recorded = state.Group(pgid, "boot-a", "42")
    assert state.owns(recorded, current) is wanted


@pytest.mark.parametrize(
    ("code", "wanted"), [(0, 0), (1, 1), (124, 124), (-1, 129), (-9, 137), (-15, 143)]
)
def test_exit_status(code: int, wanted: int) -> None:
    assert state.exit_status(code) == wanted
