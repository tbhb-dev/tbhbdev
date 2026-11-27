"""Check how recorded review deadlines account for limits and grace periods."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from agent_example import LINUX, settings
from driver_run_example import BUILD

from tbhbdev.alpha.agent.driver.decisions.metadata import Run
from tbhbdev.alpha.agent.driver.decisions.reviews import past_time_limit, review_key


@pytest.mark.parametrize(
    ("timeout", "host_minutes", "limit"),
    [(90, 4, 90), (None, 4, 240), (600, 4, 240), (None, None, 21600)],
)
def test_time_limit(timeout: int | None, host_minutes: int | None, limit: int) -> None:
    config = replace(
        settings(),
        max_run_minutes=host_minutes,
        launch_wait_seconds=120,
        kill_grace_seconds=15,
    )
    started = datetime(2026, 11, 9, tzinfo=UTC)
    wire = replace(BUILD, timeout=timeout)
    record = Run("sample", LINUX, wire, started.isoformat(), "owner")
    deadline = started + timedelta(seconds=limit + 135)
    assert not past_time_limit(config, record, deadline)
    assert past_time_limit(config, record, deadline + timedelta(seconds=1))


@pytest.mark.parametrize(
    "spelling", ["Example-Org/Example-Repo", "github.com/example-org/example-repo.git"]
)
def test_review_key_uses_canonical_repository(spelling: str) -> None:
    one = Run("one", LINUX, BUILD, "today", "owner")
    two = Run("two", LINUX, replace(BUILD, repo=spelling), "today", "owner")
    assert review_key(one) == review_key(two)
