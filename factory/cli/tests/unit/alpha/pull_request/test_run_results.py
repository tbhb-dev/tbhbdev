# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Classify workflow runs and jobs from GitHub response data."""

from datetime import UTC, datetime

import pytest

from tbhbdev.alpha.pull_request.run_results import job_is_stale


@pytest.mark.parametrize(
    ("start", "stale"),
    [("12:59:59", True), ("13:00:00", False), ("13:00:01", False)],
)
def test_a_running_job_is_stale_after_one_hour(start: str, stale: bool) -> None:
    now = datetime(2026, 1, 1, 14, tzinfo=UTC)
    assert job_is_stale(f"2026-01-01T{start}Z", now) is stale
