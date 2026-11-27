# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for reading merge evidence through gh."""

import json

import pytest
from unit.alpha.merge.fake_github import (
    APPROVED_HEAD,
    OTHER_HEAD,
    REVIEWS,
    REVIEWS_APP,
    TARGET,
    Tools,
    amend,
    commit,
    queue_rules,
    repo_reads,
    run_main,
)

from tbhbdev.alpha.merge.github import VIEW_FIELDS, check_repository, fetch


def settings_calls(tools: Tools) -> list[dict[str, object]]:
    log = tools.bin / "gh.settings.log"
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"mergeStateStatus": "UNSTABLE"}, "mergeStateStatus=UNSTABLE"),
        ({"reviewDecision": "REVIEW_REQUIRED"}, "reviewDecision=REVIEW_REQUIRED"),
        ({"reviewDecision": None}, "no reviewDecision"),
        ({"headRefOid": "short"}, "no full headRefOid"),
        ({"reviews": []}, "0 approvals at the head, need 1"),
        (
            {"reviews": [{"state": "COMMENTED"}]},
            "0 approvals at the head, need 1",
        ),
        (
            {"reviews": [REVIEWS[0] | {"author": None}]},
            "review with invalid verdict, author or commit",
        ),
        (
            {"reviews": [item | {"commit": {"oid": OTHER_HEAD}} for item in REVIEWS]},
            "0 approvals at the head, need 1",
        ),
        (
            {
                "reviews": [
                    item | {"author": {"login": REVIEWS_APP, "__typename": "User"}}
                    for item in REVIEWS
                ]
            },
            "0 approvals at the head, need 1",
        ),
        ({"statusCheckRollup": []}, "no checks to verify"),
        ({"statusCheckRollup": None}, "no usable statusCheckRollup"),
        ({"statusCheckRollup": [None]}, "invalid statusCheckRollup entry"),
        ({"commits": []}, "no commits for the current head"),
        ({"commits": [commit("short")]}, "commit with invalid SHA, message or authors"),
        ({"commits": [commit(OTHER_HEAD)]}, "commits do not end at the current head"),
        *[
            (
                {
                    "statusCheckRollup": [
                        {
                            "name": "conclusion",
                            "status": "COMPLETED",
                            "conclusion": outcome,
                        }
                    ]
                },
                f"check conclusion is {outcome.lower() or 'completed'}",
            )
            for outcome in ("SKIPPED", "NEUTRAL", "FAILURE", "")
        ],
    ],
)
def test_merge_refuses_unjudged_or_non_green_head(
    tools: Tools, fields: dict[str, object], reason: str
) -> None:
    tools.view(**fields)
    code = run_main("12")
    assert reason in str(code)
    assert len(tools.calls()) == 1


def test_merge_refuses_a_head_that_moved_since_approval(tools: Tools) -> None:
    current = "b" * 40
    tools.view(headRefOid=current)
    code = run_main("12", "--expect-head", APPROVED_HEAD)
    assert code == (
        f"not merging PR #12: expected head {APPROVED_HEAD}, "
        f"but the current head is {current}"
    )
    assert [call["argv"] for call in tools.calls()] == [
        ["pr", "view", "12", "--json", VIEW_FIELDS]
    ]


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"isDraft": True}, "draft"),
        ({"state": "MERGED"}, "merged"),
        ({"mergeable": "CONFLICTING"}, "mergeable=CONFLICTING"),
        ({"mergeStateStatus": "BLOCKED"}, "mergeStateStatus=BLOCKED"),
        ({"mergeStateStatus": "BEHIND"}, "BEHIND"),
        (
            {
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [
                    {"name": "lint", "conclusion": "FAILURE"},
                    {"context": "ci/x", "state": "ERROR"},
                    {"name": "ok", "conclusion": "SUCCESS"},
                ],
            },
            "failing checks: lint, ci/x",
        ),
    ],
)
def test_unmergeable_prs_are_refused_with_gh_s_reason(
    tools: Tools, fields: dict[str, object], reason: str
) -> None:
    tools.view(**fields)
    code = run_main("3")
    assert isinstance(code, str)
    assert code.startswith("not merging PR #3: ")
    assert reason in code
    if "statusCheckRollup" in fields:
        assert code == (
            "not merging PR #3: gh reports mergeStateStatus=BLOCKED; "
            "failing checks: lint, ci/x"
        )
    assert len(tools.calls()) == 1


def test_gh_view_failure_is_reported(tools: Tools) -> None:
    tools.gh(view_rc=1, view_err="no\nsuch pr\n")
    assert run_main("3") == "gh pr view failed: no such pr"


def test_gh_view_garbage_is_reported(tools: Tools) -> None:
    tools.gh(view_out="<html>")
    code = run_main("3")
    assert isinstance(code, str)
    assert code.startswith("gh pr view returned something that is not JSON")


def test_gh_view_requires_a_pull_request_object(tools: Tools) -> None:
    tools.gh(view_out="[]")
    assert run_main("12") == "gh pr view returned no usable pull request"


def test_gh_view_rejects_an_invalid_commit_author(tools: Tools) -> None:
    tools.view(commits=[commit() | {"authors": [{}]}])
    assert run_main("12") == "gh pr view returned invalid commit authors"


def test_missing_fields_default_to_empty_and_refuse(tools: Tools) -> None:
    tools.gh(view_out="{}")
    code = run_main("3")
    assert isinstance(code, str)
    assert "the PR is " in code


def test_gh_is_asked_for_the_author() -> None:
    assert "author" in VIEW_FIELDS.split(",")


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"url": ""},
        {"url": 7},
        {"url": "not a url"},
        {"url": "https://github.com/o/n/pull/x"},
        {"url": "http://github.com/o/n/pull/12"},
        {"url": "https://gitlab.com/o/n/pull/12"},
        {"url": "https://github.com/o/n/issues/12"},
    ],
)
def test_an_unresolved_target_refuses_before_any_read_or_scan(
    tools: Tools, fields: dict[str, object]
) -> None:
    data = json.loads(json.loads((tools.bin / "gh.spec").read_text())["view_out"])
    data.pop("url")
    tools.gh(view_out=json.dumps(data | fields), repo_out="PRIVATE\n")
    assert "can't tell which repository" in str(run_main("12"))
    assert repo_reads(tools) == []
    assert tools.scanned() == []


def test_the_view_reads_the_url() -> None:
    assert "url" in VIEW_FIELDS.split(",")


@pytest.mark.parametrize("returncode", [2, -15])
def test_fetch_reports_every_nonzero_exit_with_normalized_stderr(
    tools: Tools, returncode: int
) -> None:
    tools.gh(view_rc=returncode, view_out="{}", view_err=" \tno\n such\r\npr  \t")
    with pytest.raises(SystemExit) as info:
        fetch(12)
    assert info.value.code == "gh pr view failed: no such pr"


def test_fetch_includes_the_json_decode_error(tools: Tools) -> None:
    tools.gh(view_out="{")
    with pytest.raises(SystemExit) as info:
        fetch(12)
    assert info.value.code == (
        "gh pr view returned something that is not JSON: "
        "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
    )


def test_merge_requires_success_for_a_named_required_check(tools: Tools) -> None:
    contexts = [{"context": "lint"}]
    required = {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": contexts},
    }
    amend(tools, rules_out=json.dumps([[required]]))
    code = run_main("12")
    assert "required check lint has no success at the head" in str(code)
    assert len(tools.calls()) == 1
    tools.view(
        statusCheckRollup=[
            {"context": "conclusion", "state": "SUCCESS"},
            {"context": "lint", "state": "SUCCESS"},
        ]
    )
    amend(tools, rules_out=json.dumps([[required]]))
    assert run_main("12") == 0


@pytest.mark.parametrize(
    ("checks", "reason"),
    [
        ([], "no usable required status checks"),
        ([{"context": ""}], "invalid required status check"),
    ],
)
def test_merge_refuses_invalid_required_checks(
    tools: Tools, checks: list[dict[str, str]], reason: str
) -> None:
    rule = {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": checks},
    }
    amend(tools, rules_out=json.dumps([[rule]]))
    assert reason in str(run_main("12"))
    assert len(tools.calls()) == 1


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (
            {"reviews_rc": 1, "reviews_err": "forbidden"},
            "gh review lookup failed: forbidden",
        ),
        ({"reviews_out": "<html>"}, "gh review lookup returned invalid JSON"),
        ({"reviews_out": "{}"}, "no usable pull request review data"),
    ],
)
def test_merge_refuses_when_reviews_cannot_be_read(
    tools: Tools, spec: dict[str, object], reason: str
) -> None:
    amend(tools, **spec)
    assert reason in str(run_main("12"))
    assert len(tools.calls()) == 1


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"headRefOid": OTHER_HEAD}, "PR head changed"),
        ({"reviewDecision": "CHANGES_REQUESTED"}, "no longer reports reviewDecision"),
        (
            {"reviews": {"pageInfo": {"hasNextPage": True}, "nodes": REVIEWS}},
            "missing or incomplete review list",
        ),
    ],
)
def test_merge_refuses_when_review_evidence_changes(
    tools: Tools, fields: dict[str, object], reason: str
) -> None:
    path = tools.bin / "gh.spec"
    review = json.loads(json.loads(path.read_text())["reviews_out"])
    review["data"]["repository"]["pullRequest"].update(fields)
    amend(tools, reviews_out=json.dumps(review))
    assert reason in str(run_main("12"))
    assert len(tools.calls()) == 1


def test_the_merge_setting_is_read_from_the_pull_requests_repository(
    tools: Tools,
) -> None:
    check_repository(TARGET)
    (call,) = settings_calls(tools)
    assert call["argv"] == ["repo", "view", TARGET, "--json", "mergeCommitAllowed"]


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (
            {"settings_out": '{"mergeCommitAllowed": false}'},
            "does not allow merge commits",
        ),
        ({"settings_rc": 1, "settings_err": "boom"}, "gh repo view failed: boom"),
        ({"settings_out": "not json"}, "no usable merge settings"),
        ({"settings_out": "[1]"}, "no usable merge settings"),
        ({"settings_out": "{}"}, "no usable merge settings"),
        (
            {"settings_out": '{"mergeCommitAllowed": "yes"}'},
            "no usable merge settings",
        ),
    ],
)
def test_unknown_or_disabled_merge_setting_refuses(
    tools: Tools, spec: dict[str, object], reason: str
) -> None:
    amend(tools, **spec)
    code = run_main("12")
    assert reason in str(code)
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        (
            {"rules_rc": 1, "rules_err": "forbidden"},
            "gh branch rules lookup failed: forbidden",
        ),
        ({"rules_out": "not JSON"}, "gh returned no usable branch rules"),
        ({"rules_out": "{}"}, "gh returned no usable branch rules"),
        ({"rules_out": "[]"}, "gh returned no usable branch rules"),
        ({"rules_out": "[{}]"}, "gh returned no usable branch rules"),
        ({"rules_out": "[[{}]]"}, "gh returned no usable branch rules"),
        ({"rules_out": "[[null]]"}, "gh returned no usable branch rules"),
        (
            {"rules_out": queue_rules("squash")},
            "not merging: unsupported branch merge queue method SQUASH",
        ),
        (
            {"rules_out": queue_rules("rebase")},
            "not merging: unsupported branch merge queue method REBASE",
        ),
        (
            {"rules_out": '[[{"type":"merge_queue"}]]'},
            "not merging: unsupported branch merge queue method (MISSING)",
        ),
    ],
)
def test_invalid_queue_rules_refuse(
    tools: Tools, spec: dict[str, object], error: str
) -> None:
    amend(tools, **spec)
    assert run_main("12") == error
    assert len(tools.calls()) == 1


def test_no_base_branch_refuses(tools: Tools) -> None:
    tools.view(baseRefName=None)
    assert run_main("12") == "not merging: gh pr view returned no base branch"
    assert len(tools.calls()) == 1
