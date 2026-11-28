# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Interpret the state of a pull request in the merge queue."""

import json
from typing import cast

from tbhbdev.merge.evidence import object_fields


def parse_queue_state(output: str) -> dict[str, object]:
    """Decode the queue status response for the pull request."""
    try:
        data = json.loads(output)["data"]["repository"]["pullRequest"]
    except ValueError, KeyError, TypeError:
        error = "gh returned no usable merge queue state"
        raise SystemExit(error) from None
    state = object_fields(data)
    if state is None:
        error = "gh returned no usable merge queue state"
        raise SystemExit(error)
    return state


def queue_removal(state: dict[str, object]) -> tuple[str, str | None]:
    """Return the latest removal event's ID and reason."""
    timeline = object_fields(state.get("timelineItems")) or {}
    match cast("list[dict[str, object]]", timeline.get("nodes")):
        case [{"id": str() as event, "reason": (str() | None) as reason}]:
            return event, reason
        case []:
            return "", None
        case _:
            message = "gh returned no usable merge queue timeline"
            raise SystemExit(message)


def queue_result(state: dict[str, object], number: int, previous_removal: str) -> str:
    """Describe a successful merge or enqueue, or report a queue failure."""
    if state.get("state") == "MERGED":
        commit = object_fields(state.get("mergeCommit")) or {}
        sha = commit.get("oid")
        if isinstance(sha, str) and sha:
            return f"PR #{number} merged as {sha}"
    if state.get("state") == "OPEN" and state.get("isInMergeQueue") is True:
        return f"PR #{number} queued"
    event, reason = queue_removal(state)
    if event and event != previous_removal:
        error = f"PR #{number} dequeued: {reason or 'GitHub supplied no reason'}"
        raise SystemExit(error)
    message = (
        f"PR #{number}: GitHub reports no merge commit, queue entry, or new removal"
    )
    raise SystemExit(message)
