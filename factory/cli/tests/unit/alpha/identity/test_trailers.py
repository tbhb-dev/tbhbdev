# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""UTC parsing and timestamp fields in attribution trailers."""

import pytest

from tbhbdev.alpha.identity import trailers

EARLY = "2026-11-14T00:02:41Z"
ROOT = "sha-256-64:" + "a" * 16
CHILD = "sha-256-64:" + "c" * 16


def test_utc_formats_whole_seconds() -> None:
    assert trailers.utc(0) == "1970-01-01T00:00:00Z"
    assert trailers.utc(1_800_000_000.9) == "2027-01-15T08:00:00Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (EARLY, EARLY),
        ("2026-02-30T00:00:00Z", None),
        ("2026-11-14T24:00:00Z", None),
        ("2026-11-14t00:02:41z", None),
        ("\u0662\u0660\u0662\u0666-11-14T00:02:41Z", None),
        ("2026-11-14T00:02:41", None),
        ("2026-11-14T00:02:41+00:00", None),
        ("2026-11-14T00:02:41.5Z", None),
        (" " + EARLY, None),
        (EARLY + "Z", None),
        (None, None),
        (5, None),
    ],
)
def test_start_ok(value: object, expected: str | None) -> None:
    assert trailers.start_ok(value) == expected


def test_without_start_and_trailer_start() -> None:
    value = f"LLM harness=h;model=m;session={ROOT};started_at={EARLY};subagent={CHILD}"
    assert (
        trailers.without_start(value)
        == f"LLM harness=h;model=m;session={ROOT};subagent={CHILD}"
    )
    assert (
        trailers.without_start(f"LLM x_started_at={EARLY}")
        == f"LLM x_started_at={EARLY}"
    )
    assert trailers.trailer_start(value) == EARLY
    assert trailers.trailer_start(f"LLM x_started_at={EARLY}") == ""
    assert trailers.trailer_start("LLM harness=h") == ""
    assert trailers.trailer_start("LLM started_at=0") == ""
    assert trailers.trailer_start("LLM started_at=2026-02-30T00:00:00Z") == ""
