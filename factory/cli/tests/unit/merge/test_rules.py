# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Pure tests for repository settings and branch rules."""

import pytest

from tbhbdev.merge.evidence import EvidenceError
from tbhbdev.merge.review_gate import Pull
from tbhbdev.merge.rules import (
    parse_branch_rules,
    require_merge_commits,
    required_checks,
    requires_queue,
)


def test_repository_must_enable_merge_commits() -> None:
    require_merge_commits('{"mergeCommitAllowed": true}', "owner/repo")
    with pytest.raises(SystemExit, match="owner/repo does not allow merge commits"):
        require_merge_commits('{"mergeCommitAllowed": false}', "owner/repo")


@pytest.mark.parametrize(
    "output", ["invalid JSON", "[]", "{}", '{"mergeCommitAllowed": 1}']
)
def test_unusable_merge_setting_is_refused(output: str) -> None:
    with pytest.raises(SystemExit, match="no usable merge settings"):
        require_merge_commits(output, "owner/repo")


def test_queue_rule_is_found_on_later_pages() -> None:
    rules = parse_branch_rules(
        '[[{"type":"pull_request"}],'
        '[{"type":"merge_queue","parameters":{"merge_method":"merge"}}]]'
    )
    assert requires_queue(rules) is True
    assert (
        requires_queue(parse_branch_rules('[[], [{"type":"pull_request"}]]')) is False
    )


@pytest.mark.parametrize("method", ["REBASE", "SQUASH"])
def test_queue_requires_merge_commits(method: str) -> None:
    rules = [{"type": "merge_queue", "parameters": {"merge_method": method}}]
    with pytest.raises(
        SystemExit, match=f"unsupported branch merge queue method {method}"
    ):
        requires_queue(rules)


def test_required_checks_must_have_succeeded() -> None:
    pull = Pull(
        "a" * 40,
        [],
        [
            {"name": "lint", "status": "completed", "conclusion": "success"},
            {"name": "build", "status": "completed", "conclusion": "neutral"},
        ],
        [],
    )
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": "lint"}, {"context": "build"}]
            },
        }
    ]
    assert required_checks(rules, pull) == [
        "required check build has no success at the head"
    ]
    with pytest.raises(EvidenceError, match="no usable required status checks"):
        required_checks([{"type": "required_status_checks"}], pull)
