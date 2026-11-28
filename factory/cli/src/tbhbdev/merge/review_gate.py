# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""The review-gate check of a pull request, as a pure function.

A pull request passes at its current head when the reviews App approved it
at least once, never requested changes, and the conclusion check succeeded.
An approval from a run that wrote a commit does not count.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from tbhbdev.alpha.identity.trailers import KEY_GEN

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from re import Pattern

MIN_APPROVALS = 1

HEAD_SHA = re.compile(r"[0-9a-f]{40}")


class Review(TypedDict):
    id: int | str
    state: str
    commit_id: str
    body: str
    login: str


class Check(TypedDict):
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class Pull:
    """What the check reads of a pull request, at its head."""

    head: str
    reviews: Sequence[Review]
    checks: Sequence[Check]
    messages: Sequence[str]


@dataclass(frozen=True)
class Attribution:
    """How to read and validate attribution from a commit or review footer."""

    trailers: Callable[[str], list[tuple[str, str]]]
    attr_pattern: Pattern[str]
    harnesses: Collection[str]
    hash_pattern: Pattern[str]

    def generated(self, text: str) -> list[dict[str, str]]:
        """Read the attributes of each Generated-by trailer."""
        return [
            dict(self.attr_pattern.findall(value))
            for key, value in self.trailers(text)
            if key == KEY_GEN
        ]

    def known_harness(self, harness: str) -> bool:
        """Whether the harness name before any version is recognized."""
        return harness.lower().partition(":")[0] in self.harnesses

    def launch(self, attrs: dict[str, str]) -> str | None:
        """The launch hash in the attribution, if valid."""
        value = attrs.get("launch")
        return value if value and self.hash_pattern.fullmatch(value) else None


def review_attribution(body: str, attribution: Attribution) -> dict[str, str]:
    """Read the approving run's attribution from the trailer."""
    footer = body.rstrip().rpartition("\n")[2]
    trailers = attribution.generated(footer)
    if trailers:
        return trailers[0]
    return {}


def author_launches(
    messages: Sequence[str], attribution: Attribution
) -> tuple[set[str], list[str]]:
    """The launch hashes of the Generated-by trailers and any attribution errors."""
    launches: set[str] = set()
    reasons: list[str] = []
    for message in messages:
        for attrs in attribution.generated(message):
            if (launch := attribution.launch(attrs)) is not None:
                launches.add(launch)
            harness = attrs.get("harness", "")
            if not attribution.known_harness(harness):
                name = harness or "(none)"
                reasons.append(f"a Generated-by trailer names unknown harness {name}")
    return launches, reasons


def review_reasons(
    app: str, pull: Pull, launches: set[str], attribution: Attribution
) -> list[str]:
    """The reasons the App's reviews do not approve the current head."""
    reasons: list[str] = []
    same_run: list[str] = []
    approvals = 0
    login = app.casefold()
    for review in pull.reviews:
        if review["login"].casefold() != login or review["commit_id"] != pull.head:
            continue
        if review["state"] == "CHANGES_REQUESTED":
            reasons.append(f"review {review['id']} requests changes at the head")
        elif review["state"] == "APPROVED":
            attrs = review_attribution(review["body"], attribution)
            if not attribution.known_harness(attrs.get("harness", "")):
                reasons.append(f"review {review['id']} names no known harness")
            elif attribution.launch(attrs) in launches:
                same_run.append(
                    f"review {review['id']} came from a run that wrote a commit"
                )
            else:
                approvals += 1
    if approvals < MIN_APPROVALS:
        reasons.extend(same_run)
        reasons.append(f"{approvals} approvals at the head, need {MIN_APPROVALS}")
    return reasons


def _outcome(check: Check) -> str:
    """What a check run is: its conclusion once completed, else its status."""
    if check["status"] == "completed":
        return check["conclusion"] or check["status"]
    return check["status"]


def check_reasons(pull: Pull) -> list[str]:
    """The reasons the conclusion check has not succeeded."""
    checks = [check for check in pull.checks if check["name"] == "conclusion"]
    if not checks:
        return ["no checks to verify"]
    return [
        f"check {check['name']} is {_outcome(check)}"
        for check in checks
        if not (check["status"] == "completed" and check["conclusion"] == "success")
    ]


def evaluate(app: str, pull: Pull, attribution: Attribution) -> list[str]:
    """The reasons a pull request fails at its head; none means it passes."""
    if not HEAD_SHA.fullmatch(pull.head):
        return [f"the pull request head {pull.head!r} is not a full commit SHA"]
    launches, attribution_reasons = author_launches(pull.messages, attribution)
    reasons = review_reasons(app, pull, launches, attribution)
    reasons.extend(check_reasons(pull))
    reasons.extend(attribution_reasons)
    return reasons
