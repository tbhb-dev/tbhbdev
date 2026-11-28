# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Pure tests for decoding pull request evidence."""

import json

import pytest
from unit.merge.examples import RENOVATE_AUTHOR

from tbhbdev.merge.evidence import PullRequest, failing_checks, parse_pull_request

AGENT_EMAIL = "agent@example.com"
TARGET = "owner/repository"
AGENT_COMMITS = [
    {"authors": [{"email": "human@example.com"}]},
    {"authors": [{"email": "other@example.com"}, {"email": AGENT_EMAIL}]},
]


def test_a_clean_pr_with_odd_rollup_entries_is_fine() -> None:
    assert failing_checks("nope") == []
    assert failing_checks(["x", {"conclusion": "SUCCESS"}]) == []


@pytest.mark.parametrize("agent", [False, True])
def test_parse_records_the_pr_from_one_read(agent: bool) -> None:
    commits = (
        AGENT_COMMITS if agent else [{"authors": [{"email": "human@example.com"}]}]
    )
    data = {
        "title": "fix: retain trailers",
        "body": "description",
        "headRefOid": "deadbeef",
        "reviewDecision": "APPROVED",
        "url": f"https://github.com/{TARGET}/pull/12",
        "baseRefName": "release",
        "state": "CLOSED",
        "isDraft": True,
        "mergeable": "CONFLICTING",
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [{"name": "lint", "conclusion": "FAILURE"}],
        "author": RENOVATE_AUTHOR,
        "commits": commits,
    }
    assert parse_pull_request(json.dumps(data), AGENT_EMAIL) == PullRequest(
        title="fix: retain trailers",
        body="description",
        head="deadbeef",
        base="release",
        state="CLOSED",
        draft=True,
        mergeable="CONFLICTING",
        merge_state="BLOCKED",
        decision="APPROVED",
        rollup=[{"name": "lint", "conclusion": "FAILURE"}],
        commits=commits,
        renovate=True,
        url=f"https://github.com/{TARGET}/pull/12",
        agent=agent,
    )


def test_parse_defaults_are_exact() -> None:
    assert parse_pull_request("{}", AGENT_EMAIL) == PullRequest(
        title="",
        body="",
        head="",
        state="",
        draft=False,
        mergeable="",
        merge_state="",
        renovate=False,
        url=None,
        agent=False,
    )


def test_parse_converts_text_fields_to_strings() -> None:
    data = {
        "title": 12,
        "body": 34,
        "headRefOid": 56,
        "state": 78,
        "mergeable": 90,
        "mergeStateStatus": 123,
    }
    request = parse_pull_request(json.dumps(data), AGENT_EMAIL)
    assert (
        request.title,
        request.body,
        request.head,
        request.state,
        request.mergeable,
        request.merge_state,
    ) == ("12", "34", "56", "78", "90", "123")


@pytest.mark.parametrize("body", [None, False, 0, ""])
def test_parse_normalizes_a_falsy_body(body: object) -> None:
    assert parse_pull_request(json.dumps({"body": body}), AGENT_EMAIL).body == ""


@pytest.mark.parametrize("draft", [None, False, 0, 1, "true", "false"])
def test_parse_requires_literal_true_for_a_draft(draft: object) -> None:
    assert (
        parse_pull_request(json.dumps({"isDraft": draft}), AGENT_EMAIL).draft is False
    )


@pytest.mark.parametrize(
    "author",
    [
        {"is_bot": 1, "login": "app/tbhb-dev-renovate"},
        {"is_bot": "true", "login": "app/tbhb-dev-renovate"},
        {"is_bot": True},
        {"is_bot": True, "login": None},
        {"is_bot": True, "login": 12},
        {"is_bot": True, "login": "app/tbhb-dev-RENOVATE"},
    ],
)
def test_parse_does_not_mistake_other_author_metadata_for_renovate(
    author: object,
) -> None:
    assert (
        parse_pull_request(json.dumps({"author": author}), AGENT_EMAIL).renovate
        is False
    )
