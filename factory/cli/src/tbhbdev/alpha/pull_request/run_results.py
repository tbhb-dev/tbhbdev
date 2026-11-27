# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Classify workflow runs and jobs for pull request reruns."""

import datetime as dt
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

STUCK_AFTER = dt.timedelta(minutes=60)


def job_is_stale(started_at: str, now: dt.datetime) -> bool:
    """Whether a running job started more than an hour ago."""
    return now - dt.datetime.fromisoformat(started_at) > STUCK_AFTER


def in_progress(
    runs: Sequence[dict[str, object]], head: str
) -> Iterator[dict[str, object]]:
    """Yield unfinished runs for this head in the order gh returned them."""
    return (
        item
        for item in runs
        if item["headSha"] == head and item["status"] == "in_progress"
    )


def failures(pages: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Return failed runs from all of the response pages."""
    return [
        item
        for page in pages
        for item in cast("list[dict[str, object]]", page["workflow_runs"])
        if item["conclusion"] == "failure"
    ]
