# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse and classify the checks GitHub reports."""

from typing import cast

GREEN_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
UNFINISHED = frozenset({"QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "PENDING"})


def check_kind(job: object) -> tuple[dict[str, object], str, str]:
    """A rollup entry as a mapping, its kind (CheckRun or StatusContext) and name.

    Raises:
        SystemExit: the entry isn't a mapping, has no usable name or context, or
            names both without a ``__typename``.
    """
    if not isinstance(job, dict):
        message = f"a check of the rollup isn't an object: {job!r}"
        raise SystemExit(message)
    job = cast("dict[str, object]", job)  # pragma: no mutate
    kind = job.get("__typename")
    if kind is None and (job.get("name") is None) != (job.get("context") is None):
        kind = "CheckRun" if job.get("context") is None else "StatusContext"
    name = job.get("context" if kind == "StatusContext" else "name")
    if (
        kind not in ("CheckRun", "StatusContext")
        or not isinstance(name, str)
        or not name
    ):
        message = f"a check of the rollup has no usable name or context: {job}"
        raise SystemExit(message)
    return job, kind, name


def status_outcome(name: str, state: object) -> tuple[str, str, str]:
    """A commit status reads only its state, and only SUCCESS is green."""
    label = state if isinstance(state, str) and state else "(none)"
    if state == "SUCCESS":
        return name, label, "green"
    return name, label, "pending" if state == "PENDING" else "failed"


def check_outcome(job: object) -> tuple[str, str, str]:
    """A check's name, the label to print and its kind: green, pending or failed.

    A check run reads its conclusion only once its status is COMPLETED, and is
    pending only for an unfinished status. A commit status reads from its state.
    Anything outside the known values is a failure, printed as written.
    """
    job, kind, name = check_kind(job)
    if kind == "StatusContext":
        return status_outcome(name, job.get("state"))
    status = job.get("status")
    value = job.get("conclusion") if status == "COMPLETED" else status
    label = value if isinstance(value, str) and value else "(none)"
    if isinstance(status, str) and status in UNFINISHED:
        return name, "PENDING", "pending"
    green = status == "COMPLETED" and label in GREEN_CONCLUSIONS
    return name, label, "green" if green else "failed"


def check_nodes(page: dict[str, object]) -> list[object]:
    """Read a page of commit checks; a null rollup means none have started.

    Raises:
        SystemExit: the response doesn't contain a list of checks.
    """
    node: object = page
    for key in (
        "data",
        "repository",
        "object",
        "statusCheckRollup",
        "contexts",
        "nodes",
    ):
        if not isinstance(node, dict):
            message = "cannot read checks: malformed GraphQL response"
            raise SystemExit(message)
        node = cast("dict[str, object]", node).get(key)  # pragma: no mutate
        if key == "statusCheckRollup" and node is None:
            return []
    if not isinstance(node, list):
        message = "cannot read checks: contexts nodes aren't a list"
        raise SystemExit(message)
    return cast("list[object]", node)  # pragma: no mutate
