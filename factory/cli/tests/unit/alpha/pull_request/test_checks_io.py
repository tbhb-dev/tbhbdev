# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request checks."""

import json
import re
from typing import cast

import pytest
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_test_support import World, gh_object, run_main

from tbhbdev.alpha.pull_request import checks as pr_checks

host_names = pr_test_support.host_names
world = pr_test_support.world


def check_page(jobs: object) -> dict[str, object]:
    return {
        "data": {
            "repository": {
                "object": {"statusCheckRollup": {"contexts": {"nodes": jobs}}}
            }
        }
    }


@pytest.mark.parametrize(
    ("jobs", "expected", "code"),
    [
        (
            [
                {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "unit tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "lint: SUCCESS\nunit tests: SUCCESS\n",
            0,
        ),
        (
            [
                {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "unit tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "lint: FAILURE\nunit tests: SUCCESS\n",
            1,
        ),
        (
            [{"name": "tests", "status": "QUEUED", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "IN_PROGRESS", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "WAITING", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "REQUESTED", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "PENDING", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "IN_PROGRESS", "conclusion": None}],
            "tests: PENDING\n",
            3,
        ),
        (
            [
                {"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "b", "status": "QUEUED", "conclusion": None},
            ],
            "a: SUCCESS\nb: PENDING\n",
            3,
        ),
        (
            [
                {"name": "a", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "b", "status": "QUEUED", "conclusion": None},
            ],
            "a: FAILURE\nb: PENDING\n",
            1,
        ),
        (
            [{"name": "optional", "status": "COMPLETED", "conclusion": "SKIPPED"}],
            "optional: SKIPPED\n",
            0,
        ),
        (
            [{"name": "optional", "status": "COMPLETED", "conclusion": "NEUTRAL"}],
            "optional: NEUTRAL\n",
            0,
        ),
        ([{"context": "external CI", "state": "SUCCESS"}], "external CI: SUCCESS\n", 0),
        ([{"context": "external CI", "state": "FAILURE"}], "external CI: FAILURE\n", 1),
        ([{"context": "external CI", "state": "ERROR"}], "external CI: ERROR\n", 1),
        (
            [{"context": "external CI", "state": "EXPECTED"}],
            "external CI: EXPECTED\n",
            1,
        ),
        ([{"context": "external CI", "state": "PENDING"}], "external CI: PENDING\n", 3),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "TIMED_OUT"}],
            "tests: TIMED_OUT\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "CANCELLED"}],
            "tests: CANCELLED\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "ACTION_REQUIRED"}],
            "tests: ACTION_REQUIRED\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "STARTUP_FAILURE"}],
            "tests: STARTUP_FAILURE\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "STALE"}],
            "tests: STALE\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "success"}],
            "tests: success\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "MYSTERY"}],
            "tests: MYSTERY\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": ""}],
            "tests: (none)\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": None}],
            "tests: (none)\n",
            1,
        ),
        (
            [{"name": "tests", "status": "in_progress", "conclusion": ""}],
            "tests: in_progress\n",
            1,
        ),
        (
            [{"name": "tests", "status": "MYSTERY", "conclusion": ""}],
            "tests: MYSTERY\n",
            1,
        ),
        ([{"name": "tests"}], "tests: (none)\n", 1),
        *[
            ([{"name": "tests", "status": status, "conclusion": "FAILURE"}], out, 1)
            for status, out in [
                ("SUCCESS", "tests: SUCCESS\n"),
                ("NEUTRAL", "tests: NEUTRAL\n"),
                ("SKIPPED", "tests: SKIPPED\n"),
            ]
        ],
        *[
            ([{"context": "ci", "state": state, "conclusion": "SUCCESS"}], out, 1)
            for state, out in [
                ("NEUTRAL", "ci: NEUTRAL\n"),
                ("SKIPPED", "ci: SKIPPED\n"),
                ("COMPLETED", "ci: COMPLETED\n"),
            ]
        ],
        (
            [
                {"name": "slow", "status": "QUEUED"},
                {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "slow: PENDING\ntests: SUCCESS\n",
            3,
        ),
        (
            [
                {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "slow", "status": "QUEUED"},
                {"name": "other", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "tests: SUCCESS\nslow: PENDING\nother: SUCCESS\n",
            3,
        ),
        ([{"name": "tests", "status": ["QUEUED"]}], "tests: (none)\n", 1),
        ([{"name": "tests", "status": {"a": 1}}], "tests: (none)\n", 1),
        ([{"context": "ci", "state": ["PENDING"]}], "ci: (none)\n", 1),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "PENDING"}],
            "tests: PENDING\n",
            1,
        ),
        (
            [
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "context": None,
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
            "tests: SUCCESS\n",
            0,
        ),
        (
            [
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "context": "other",
                    "state": "SUCCESS",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ],
            "tests: FAILURE\n",
            1,
        ),
        (
            [
                {
                    "__typename": "StatusContext",
                    "name": "ignored",
                    "context": "external CI",
                    "state": "SUCCESS",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ],
            "external CI: SUCCESS\n",
            0,
        ),
        ([{"context": "external CI", "state": "mystery"}], "external CI: mystery\n", 1),
    ],
)
def test_checks_prints_the_head_and_each_job(
    world: World,
    capsys: pytest.CaptureFixture[str],
    jobs: list[dict[str, object]] | None,
    expected: str,
    code: int,
) -> None:
    sha = "a1b2c3d4" * 5
    data = {"headRefOid": sha, "statusCheckRollup": jobs}
    world.fake(
        "gh",
        cases={
            "pr view": {"rc": 0, "stdout": json.dumps(data), "stderr": ""},
            "api graphql": {
                "rc": 0,
                "stdout": json.dumps([check_page(data["statusCheckRollup"])]),
                "stderr": "",
            },
        },
    )
    assert run_main("checks", "45") == code
    captured = capsys.readouterr()
    lines = "".join(f"{sha} {line}\n" for line in expected.splitlines())
    assert (captured.out, captured.err) == (f"{sha}\n{lines}", "")
    call, api = world.calls("gh")
    assert call["argv"] == [
        "pr",
        "view",
        "45",
        "--json",
        "headRefOid",
    ]
    assert call["stdin"] == ""
    assert call["cwd"] == str(world.root)
    assert api["argv"] == [
        *pr_checks.HEAD_CHECKS_CALL,
        "--raw-field",
        f"head={sha}",
        "--raw-field",
        f"query={pr_checks.HEAD_CHECKS_QUERY}",
    ]
    assert "object(oid: $head)" in pr_checks.HEAD_CHECKS_QUERY
    assert "contexts(first: 100, after: $endCursor)" in pr_checks.HEAD_CHECKS_QUERY
    assert "pageInfo { hasNextPage endCursor }" in pr_checks.HEAD_CHECKS_QUERY
    assert "... on CheckRun { name status conclusion }" in pr_checks.HEAD_CHECKS_QUERY
    assert "... on StatusContext { context state }" in pr_checks.HEAD_CHECKS_QUERY


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (
            {"rc": 1, "stdout": "{}", "stderr": "request\n failed\n"},
            "request failed",
        ),
        ({"rc": 0, "stdout": '{"data": null}\n', "stderr": ""}, '{"data": null}'),
    ],
)
def test_checks_refuses_an_unreadable_check_list(
    world: World,
    capsys: pytest.CaptureFixture[str],
    answer: dict[str, object],
    why: str,
) -> None:
    sha = "a1b2c3d4" * 5
    world.fake(
        "gh",
        cases={"pr view": gh_object({"headRefOid": sha}), "api graphql": answer},
    )
    with pytest.raises(SystemExit, match=f"^gh api graphql failed: {re.escape(why)}$"):
        run_main("checks", "45")
    assert capsys.readouterr() == (f"{sha}\n", "")


def test_checks_waits_without_any_check(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = "a1b2c3d4" * 5
    world.fake(
        "gh",
        cases={
            "pr view": gh_object({"headRefOid": sha}),
            "api graphql": {
                "rc": 0,
                "stdout": json.dumps([check_page([])]),
                "stderr": "",
            },
        },
    )
    assert run_main("checks", "45") == 3
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        f"{sha}\n",
        f"no checks on PR 45 at {sha}; pending\n",
    )


def test_head_with_no_conclusion_run_exits_pending(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    head = "a" * 40
    stale = {
        "name": "tests",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
    }
    world.fake(
        "gh",
        cases={
            "pr view": gh_object({"headRefOid": head, "statusCheckRollup": [stale]}),
            "api graphql": {"rc": 0, "stdout": "[]", "stderr": ""},
        },
    )
    assert run_main("checks", "45") == 3
    captured = capsys.readouterr()
    assert captured.out == f"{head}\n"
    assert "no checks" in captured.err
    assert f"head={head}" in cast("list[str]", world.calls("gh")[1]["argv"])


def test_checks_includes_failures_from_later_pages(world: World) -> None:
    world.fake(
        "gh",
        cases={
            "pr view": {"rc": 0, "stdout": '{"headRefOid": "abc"}', "stderr": ""},
            "api graphql": {
                "rc": 0,
                "stdout": json.dumps(
                    [
                        check_page(
                            [
                                {
                                    "name": "first",
                                    "status": "COMPLETED",
                                    "conclusion": "SUCCESS",
                                }
                            ]
                        ),
                        check_page(
                            [
                                {
                                    "name": "last",
                                    "status": "COMPLETED",
                                    "conclusion": "FAILURE",
                                }
                            ]
                        ),
                    ]
                ),
                "stderr": "",
            },
        },
    )
    assert run_main("checks", "45") == 1


@pytest.mark.parametrize(
    "job",
    [
        {"status": "COMPLETED"},
        {"name": ""},
        {"name": 4},
        {"__typename": "StatusContext", "name": "tests", "state": "SUCCESS"},
        {"__typename": "Other", "name": "tests"},
        {"__typename": ["CheckRun"], "name": "tests"},
        {"__typename": {"x": 1}, "name": "tests"},
        {"name": "tests", "context": "other", "state": "SUCCESS"},
    ],
)
def test_checks_names_a_check_without_a_name(
    world: World, job: dict[str, object]
) -> None:
    data = {"headRefOid": "a1b2c3d4" * 5, "statusCheckRollup": [job]}
    world.fake(
        "gh",
        cases={
            "pr view": {"rc": 0, "stdout": json.dumps(data), "stderr": ""},
            "api graphql": {
                "rc": 0,
                "stdout": json.dumps([check_page(data["statusCheckRollup"])]),
                "stderr": "",
            },
        },
    )
    with pytest.raises(
        SystemExit, match=r"^a check of the rollup has no usable name or context"
    ):
        run_main("checks", "45")


@pytest.mark.parametrize("rollup", [7, True, "bad", {"a": 1}])
def test_checks_refuses_a_rollup_that_is_not_a_list(
    world: World, rollup: object
) -> None:
    data = {"headRefOid": "a1b2c3d4" * 5, "statusCheckRollup": rollup}
    world.fake(
        "gh",
        cases={
            "pr view": {"rc": 0, "stdout": json.dumps(data), "stderr": ""},
            "api graphql": {
                "rc": 0,
                "stdout": json.dumps([check_page(data["statusCheckRollup"])]),
                "stderr": "",
            },
        },
    )
    with pytest.raises(
        SystemExit, match=r"^cannot read checks: contexts nodes aren't a list$"
    ):
        run_main("checks", "45")


@pytest.mark.parametrize("job", [None, "bad", 7, []])
def test_checks_refuses_an_entry_that_is_not_an_object(
    world: World, job: object
) -> None:
    data = {"headRefOid": "a1b2c3d4" * 5, "statusCheckRollup": [job]}
    world.fake(
        "gh",
        cases={
            "pr view": {"rc": 0, "stdout": json.dumps(data), "stderr": ""},
            "api graphql": {
                "rc": 0,
                "stdout": json.dumps([check_page(data["statusCheckRollup"])]),
                "stderr": "",
            },
        },
    )
    with pytest.raises(SystemExit, match=r"^a check of the rollup isn't an object"):
        run_main("checks", "45")


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (
            {"rc": 1, "stdout": "", "stderr": "no pull requests found for branch 45\n"},
            "no pull requests found for branch 45",
        ),
        ({"rc": 1, "stdout": "{}", "stderr": "not found"}, "not found"),
        ({"rc": 1, "stdout": "not found\n", "stderr": ""}, "not found"),
        ({"rc": 0, "stdout": "not json\n", "stderr": ""}, "not json"),
        ({"rc": 0, "stdout": "[]", "stderr": ""}, "[]"),
    ],
)
def test_checks_stops_when_the_pr_cannot_be_read(
    world: World,
    capsys: pytest.CaptureFixture[str],
    answer: dict[str, object],
    why: str,
) -> None:
    world.fake("gh", cases={"pr view": answer})
    with pytest.raises(
        SystemExit, match=f"^cannot read the head of PR 45: {re.escape(why)}$"
    ):
        run_main("checks", "45")
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


@pytest.mark.parametrize("argv", [(), ("abc",)])
def test_checks_requires_a_pr_number(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main("checks", *argv)
    assert info.value.code == 2
