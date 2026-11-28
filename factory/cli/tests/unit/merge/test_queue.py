# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Pure tests for interpreting merge queue responses."""

import pytest

from tbhbdev.merge.queue import parse_queue_state, queue_removal, queue_result


@pytest.mark.parametrize(
    "output", ["not JSON", "{}", '{"data":{"repository":{"pullRequest":null}}}']
)
def test_unusable_queue_state_is_refused(output: str) -> None:
    with pytest.raises(SystemExit, match="no usable merge queue state"):
        parse_queue_state(output)


def test_queue_state_has_the_latest_removal() -> None:
    state = parse_queue_state(
        '{"data":{"repository":{"pullRequest":{"timelineItems":{"nodes":'
        '[{"id":"old","reason":"An earlier removal"}]}}}}}'
    )
    assert queue_removal(state) == ("old", "An earlier removal")


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"state": "OPEN", "isInMergeQueue": True}, "PR #12 queued"),
        (
            {"state": "MERGED", "mergeCommit": {"oid": "def456"}},
            "PR #12 merged as def456",
        ),
    ],
)
def test_successful_queue_result(state: dict[str, object], message: str) -> None:
    assert queue_result(state, 12, "old") == message


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (
            {
                "state": "OPEN",
                "timelineItems": {"nodes": [{"id": "new", "reason": "CI failed"}]},
            },
            "PR #12 dequeued: CI failed",
        ),
        (
            {
                "state": "OPEN",
                "timelineItems": {"nodes": [{"id": "new", "reason": None}]},
            },
            "PR #12 dequeued: GitHub supplied no reason",
        ),
        (
            {
                "state": "OPEN",
                "timelineItems": {"nodes": [{"id": "old", "reason": "Old"}]},
            },
            "PR #12: GitHub reports no merge commit, queue entry, or new removal",
        ),
        (
            {
                "state": "MERGED",
                "mergeCommit": {"oid": ""},
                "timelineItems": {"nodes": []},
            },
            "PR #12: GitHub reports no merge commit, queue entry, or new removal",
        ),
        ({"state": "OPEN"}, "gh returned no usable merge queue timeline"),
    ],
)
def test_unsuccessful_queue_result(state: dict[str, object], message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        queue_result(state, 12, "old")
