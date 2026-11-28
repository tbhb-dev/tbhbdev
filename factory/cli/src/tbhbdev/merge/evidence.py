# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Decode GitHub pull request and review evidence for the current head."""

import json
from dataclasses import dataclass
from typing import cast

from tbhbdev.merge.review_gate import HEAD_SHA, Check, Review

PASSING_STATES = frozenset({"CLEAN", "HAS_HOOKS"})
FAILED_CONCLUSIONS = (
    "FAILURE",
    "CANCELLED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
)
FAILED_STATES = ("FAILURE", "ERROR")
# gh pr view returns up to 100 commits and check contexts without page data.
VIEW_LIMIT = 100
RENOVATE = "renovate"


@dataclass(frozen=True)
class PullRequest:
    """What gh reports about the pull request."""

    title: str
    body: str
    head: str
    state: str
    draft: bool
    mergeable: str
    merge_state: str
    renovate: bool = False
    url: object = None
    agent: bool = False
    base: str = ""
    decision: object = None
    rollup: object = None
    commits: object = None


class EvidenceError(ValueError):
    """GitHub did not return usable evidence for the merge decision."""


def refusals(request: PullRequest) -> list[str]:
    """Why this PR may not merge now, in gh's own words."""
    reasons: list[str] = []
    if request.state != "OPEN":
        reasons.append(f"the PR is {request.state.lower()}")
    if request.draft:
        reasons.append("the PR is a draft")
    if request.mergeable != "MERGEABLE":
        reasons.append(f"gh reports mergeable={request.mergeable}")
    if request.merge_state not in PASSING_STATES:
        reasons.append(f"gh reports mergeStateStatus={request.merge_state}")
    if reasons and (failed := failing_checks(request.rollup)):
        reasons.append("failing checks: " + ", ".join(failed))
    return reasons


def failed_name(entry: object) -> str | None:
    """The name of a failed statusCheckRollup entry, or None."""
    fields = cast("dict[str, object]", entry) if isinstance(entry, dict) else {}
    if (
        fields.get("conclusion") in FAILED_CONCLUSIONS
        or fields.get("state") in FAILED_STATES
    ):
        return str(fields.get("name") or fields.get("context"))
    return None


def failing_checks(rollup: object) -> list[str]:
    """Names of the failed entries in gh's statusCheckRollup."""
    entries = cast("list[object]", rollup) if isinstance(rollup, list) else []
    return [name for entry in entries if (name := failed_name(entry))]


def view_entries(value: object, field: str) -> list[object]:
    """Read a gh pr view list only when it is known to be complete."""
    if not isinstance(value, list) or len(value) >= VIEW_LIMIT:
        message = f"gh pr view returned no usable {field} or truncated it"
        raise EvidenceError(message)
    return cast("list[object]", value)


def check_entry(entry: object) -> Check:
    match cast("dict[str, object]", entry):
        case {
            "name": str(name),
            "status": str(status),
            "conclusion": (str() | None) as conclusion,
        } if name and status:
            return {
                "name": name,
                "status": status.lower(),
                "conclusion": conclusion.lower() if conclusion else None,
            }
        case {"context": str(name), "state": str(state)} if name and state:
            return {"name": name, "status": "completed", "conclusion": state.lower()}
        case _:
            message = "gh pr view returned an invalid statusCheckRollup entry"
            raise EvidenceError(message)


def commit_messages(entries: list[object], head: str) -> list[str]:
    if not entries:
        message = "gh pr view returned no commits for the current head"
        raise EvidenceError(message)
    messages: list[str] = []
    oid = ""
    for entry in entries:
        match cast("dict[str, object]", entry):
            case {
                "oid": str(oid),
                "messageHeadline": str(title),
                "messageBody": str(body),
                "authors": list(authors),
            } if HEAD_SHA.fullmatch(oid) and all(
                isinstance((object_fields(row) or {}).get("email"), str)
                for row in cast("list[object]", authors)
            ):
                messages.append(f"{title}\n\n{body}")
            case _:
                message = (
                    "gh pr view returned a commit with invalid SHA, message or authors"
                )
                raise EvidenceError(message)
    if oid != head:
        message = "gh pr view commits do not end at the current head"
        raise EvidenceError(message)
    return messages


def app_reviews(rows: list[object]) -> list[Review]:
    reviews: list[Review] = []
    for row in rows:
        fields = object_fields(row) or {}
        state = fields.get("state")
        if state in ("COMMENTED", "DISMISSED", "PENDING"):
            continue
        author = object_fields(fields.get("author")) or {}
        commit = object_fields(fields.get("commit")) or {}
        login, kind, oid = (
            author.get("login"),
            author.get("__typename"),
            commit.get("oid"),
        )
        key, body = fields.get("id"), fields.get("body")
        if (
            not isinstance(state, str)
            or state not in ("APPROVED", "CHANGES_REQUESTED")
            or not isinstance(login, str)
            or not isinstance(kind, str)
            or not isinstance(oid, str)
            or not HEAD_SHA.fullmatch(oid)
            or not isinstance(key, str)
            or not isinstance(body, str)
        ):
            message = "GitHub returned a review with invalid verdict, author or commit"
            raise EvidenceError(message)
        if kind == "Bot":
            reviews.append(
                {
                    "id": key,
                    "state": state,
                    "body": body,
                    "commit_id": oid,
                    "login": login,
                }
            )
    return reviews


def review_snapshot(output: str, head: str) -> list[Review]:
    """Decode the complete review list for the expected head."""
    try:
        data: object = json.loads(output)
    except ValueError:
        error = "gh review lookup returned invalid JSON"
        raise SystemExit(error) from None
    connection = object_fields(data) or {}
    for key in ("data", "repository", "pullRequest"):
        connection = object_fields(connection.get(key)) or {}
    if not connection:
        message = "gh review lookup returned no usable pull request review data"
        raise EvidenceError(message)
    if connection.get("headRefOid") != head:
        message = "the PR head changed or is missing in the review data"
        raise EvidenceError(message)
    if connection.get("reviewDecision") != "APPROVED":
        message = "GitHub no longer reports reviewDecision=APPROVED"
        raise EvidenceError(message)
    page = object_fields(connection.get("reviews")) or {}
    info = object_fields(page.get("pageInfo")) or {}
    rows = page.get("nodes")
    if info.get("hasNextPage") is not False or not isinstance(rows, list):
        message = "gh review lookup returned a missing or incomplete review list"
        raise EvidenceError(message)
    return app_reviews(cast("list[object]", rows))


def approval_reasons(request: PullRequest, expected: str | None) -> list[str]:
    """Require a valid head, any caller-supplied match, and GitHub's approval."""
    head = request.head
    if expected is not None and expected != head:
        return [f"expected head {expected}, but the current head is {head}"]
    if not HEAD_SHA.fullmatch(head):
        return ["gh pr view returned no full headRefOid"]
    if not isinstance(request.decision, str) or not request.decision:
        return ["gh pr view returned no reviewDecision"]
    if request.decision != "APPROVED":
        return [f"gh reports reviewDecision={request.decision}"]
    return []


def object_fields(value: object) -> dict[str, object] | None:
    """Return the fields of a JSON object, if present."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def parse_pull_request(output: str, agent_email: str) -> PullRequest:
    """Read the fields of a pull request view response."""
    try:
        raw: object = json.loads(output)
    except ValueError as exc:
        error = f"gh pr view returned something that is not JSON: {exc}"
        raise SystemExit(error) from exc
    data = object_fields(raw)
    if data is None:
        error = "gh pr view returned no usable pull request"
        raise SystemExit(error)
    who = object_fields(data.get("author")) or {}
    base = data.get("baseRefName")
    commits = cast("list[dict[str, list[dict[str, str]]]]", data.get("commits", []))
    try:
        agent = any(
            author["email"] == agent_email
            for commit in commits
            for author in commit["authors"]
        )
    except KeyError, TypeError:
        error = "gh pr view returned invalid commit authors"
        raise SystemExit(error) from None
    return PullRequest(
        agent=agent,
        renovate=who.get("is_bot") is True and RENOVATE in str(who.get("login")),
        title=str(data.get("title", "")),
        body=str(data.get("body") or ""),
        head=str(data.get("headRefOid", "")),
        base=base if isinstance(base, str) else "",
        state=str(data.get("state", "")),
        draft=data.get("isDraft") is True,
        mergeable=str(data.get("mergeable", "")),
        merge_state=str(data.get("mergeStateStatus", "")),
        url=data.get("url"),
        decision=data.get("reviewDecision"),
        rollup=data.get("statusCheckRollup"),
        commits=data.get("commits"),
    )
