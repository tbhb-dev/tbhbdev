# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:ignore Ftopic

"""Tests for pull request delete branch."""

import json
from typing import cast

import pytest
from fake_tool import PR_ORIGIN as ORIGIN
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_test_support import (
    BRANCH_LOOKUP,
    World,
    branch_cases,
    run_main,
)

host_names = pr_test_support.host_names
world = pr_test_support.world


@pytest.mark.parametrize(
    "argv",
    [
        ["delete-branch"],
        ["delete-branch", "abc"],
    ],
)
def test_delete_branch_requires_a_numeric_pr_number(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        run_main(*argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_delete_branch_deletes_only_the_origin_pr_head(
    world: World, state: str
) -> None:
    world.fake(
        "gh", cases=branch_cases(state=state), stdout="deleted\n", stderr="warn\n"
    )
    assert run_main("delete-branch", "45") == 0
    calls = world.calls("gh.repo") + world.calls("gh")
    assert [call["argv"] for call in calls] == [
        ["repo", "view", ORIGIN, "--json", "nameWithOwner,url"],
        [
            *["pr", "view", "45", "--repo", "github.com/tbhb-dev/example"],
            "--json",
            "state,headRefName,isCrossRepository",
        ],
        [
            "api",
            "--hostname",
            "github.com",
            "repos/tbhb-dev/example/branches/agent%2Ftopic",
        ],
        [
            *["api", "--hostname", "github.com"],
            *["--method", "DELETE"],
            "repos/tbhb-dev/example/git/refs/heads/agent%2Ftopic",
        ],
    ]
    assert all(call["stdin"] == "" for call in calls)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"state": "OPEN"}, "PR 45 is not closed or merged"),
        ({"state": None}, "PR 45 is not closed or merged"),
        ({"isCrossRepository": True}, "PR 45 head is not on origin"),
        ({"isCrossRepository": None}, "PR 45 head is not on origin"),
        ({"headRefName": "main"}, "refusing to delete main"),
        ({"headRefName": ""}, "cannot read the head branch of PR 45"),
        ({"headRefName": None}, "cannot read the head branch of PR 45"),
        ({"headRefName": "topic/../main"}, "invalid head branch of PR 45"),
    ],
)
def test_delete_branch_refuses_unsafe_prs(
    world: World, changes: dict[str, object], message: str
) -> None:
    world.fake("gh", cases=branch_cases(**changes))
    with pytest.raises(SystemExit, match=f"^{message}$"):
        run_main("delete-branch", "45")
    assert len(world.calls("gh")) == 1


@pytest.mark.parametrize("protected", [True, None, "false", 0])
def test_delete_branch_refuses_protected_or_unknown_branches(
    world: World, protected: object
) -> None:
    cases = branch_cases()
    cases[BRANCH_LOOKUP]["stdout"] = json.dumps(
        {"name": "agent/topic", "protected": protected}
    )
    world.fake("gh", cases=cases)
    with pytest.raises(
        SystemExit, match=r"^branch agent/topic is protected or unreadable$"
    ):
        run_main("delete-branch", "45")
    assert len(world.calls("gh")) == 2


@pytest.mark.parametrize(
    "lookup",
    ["repo view", "pr view", BRANCH_LOOKUP],
)
@pytest.mark.parametrize("output", ["not JSON", "[]", "{}"])
@pytest.mark.parametrize("rc", [0, 3])
def test_delete_branch_refuses_failed_or_malformed_lookups(
    world: World, lookup: str, output: str, rc: int
) -> None:
    cases = branch_cases()
    cases[lookup] = {"rc": rc, "stdout": output, "stderr": "boom\n"}
    world.fake("gh", cases=cases)
    with pytest.raises(SystemExit):
        run_main("delete-branch", "45")
    assert all(
        "DELETE" not in cast("list[str]", call["argv"]) for call in world.calls("gh")
    )


def test_delete_branch_requires_origin(world: World) -> None:
    world.git("remote", "remove", "origin")
    with pytest.raises(SystemExit, match=r"^cannot read origin$"):
        run_main("delete-branch", "45")
    assert world.calls("gh") == []
    assert world.calls("gh.repo") == []


@pytest.mark.parametrize(
    "url",
    [None, "https://example.test/tbhb-dev/example", "https://github.com/other/repo"],
)
def test_delete_branch_requires_a_matching_github_origin(
    world: World, url: object
) -> None:
    cases = branch_cases()
    cases["repo view"]["stdout"] = json.dumps(
        {"nameWithOwner": "tbhb-dev/example", "url": url}
    )
    world.fake("gh", cases=cases)
    with pytest.raises(
        SystemExit, match=r"^origin must name a repository on github.com$"
    ):
        run_main("delete-branch", "45")
    assert world.calls("gh") == []


def test_delete_branch_refuses_mismatched_branch_metadata(world: World) -> None:
    cases = branch_cases()
    cases[BRANCH_LOOKUP]["stdout"] = json.dumps({"name": "main", "protected": False})
    world.fake("gh", cases=cases)
    with pytest.raises(
        SystemExit, match=r"^branch agent/topic is protected or unreadable$"
    ):
        run_main("delete-branch", "45")
    assert len(world.calls("gh")) == 2


def test_delete_branch_passes_gh_output_and_exit_code_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", cases=branch_cases(), rc=3, stdout="out\n", stderr="err\n")
    assert run_main("delete-branch", "45") == 3
    assert capsys.readouterr() == ("out\n", "err\n")
