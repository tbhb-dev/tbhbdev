# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request rerun."""

import datetime as dt
import json
from typing import cast
from unittest.mock import Mock

import pytest
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_test_support import World, gh_list, gh_object, run_main

from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import rerun as pr_rerun

host_names = pr_test_support.host_names
world = pr_test_support.world


@pytest.mark.parametrize("rc", [0, 3])
def test_rerun_retries_failed_jobs_of_the_current_head(
    world: World, capsys: pytest.CaptureFixture[str], rc: int
) -> None:
    head = "a" * 40
    world.fake(
        "gh",
        rc=rc,
        stdout="out\n",
        stderr="err\n",
        cases={
            "pr view": gh_object({"headRefOid": head}),
            "api repos/{owner}/{repo}/actions/runs?": gh_list(
                [{"workflow_runs": [{"id": 123, "conclusion": "failure"}]}]
            ),
        },
    )
    assert run_main("rerun", "45") == rc
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("out\n", "err\n")
    calls = world.calls("gh")
    assert [call["argv"] for call in calls] == [
        ["pr", "view", "45", "--json", "headRefOid"],
        [
            "api",
            f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={head}&per_page=100",
            "--paginate",
            "--slurp",
        ],
        ["run", "rerun", "123", "--failed"],
    ]
    assert all(call["stdin"] == "" for call in calls)


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [{"id": 123, "conclusion": "success"}],
        [{"id": 123, "conclusion": ""}],
        [
            {"id": 123, "conclusion": "success"},
            {"id": 122, "conclusion": "cancelled"},
        ],
    ],
)
def test_rerun_reports_no_failed_run(
    world: World, capsys: pytest.CaptureFixture[str], runs: list[dict[str, object]]
) -> None:
    world.fake(
        "gh",
        cases={
            "pr view": gh_object({"headRefOid": "a" * 40}),
            "api repos/{owner}/{repo}/actions/runs?": gh_list(
                [{"workflow_runs": runs}]
            ),
        },
    )
    assert run_main("rerun", "45") == 1
    assert capsys.readouterr().err == "no failed run for the current head of PR 45\n"
    assert len(world.calls("gh")) == 2


@pytest.mark.parametrize(("first_rc", "last_rc"), [(0, 0), (3, 0), (0, 4), (3, 4)])
def test_rerun_retries_failed_run_when_newest_passed(
    world: World, first_rc: int, last_rc: int
) -> None:
    runs: list[dict[str, object]] = [
        {"id": 125, "conclusion": "success"},
        {"id": 124, "conclusion": "failure"},
        {"id": 123, "conclusion": ""},
        {"id": 122, "conclusion": "failure"},
    ]
    world.fake(
        "gh",
        cases={
            "pr view": gh_object({"headRefOid": "a" * 40}),
            "api repos/{owner}/{repo}/actions/runs?": gh_list(
                [{"workflow_runs": runs[:2]}, {"workflow_runs": runs[2:]}]
            ),
            "run rerun 124": {"rc": first_rc, "stdout": "", "stderr": ""},
            "run rerun 122": {"rc": last_rc, "stdout": "", "stderr": ""},
            "run list": gh_list([{"databaseId": 125, "conclusion": "success"}]),
        },
    )
    assert run_main("rerun", "45") == (first_rc or last_rc)
    assert [call["argv"] for call in world.calls("gh")][2:] == [
        ["run", "rerun", "124", "--failed"],
        ["run", "rerun", "122", "--failed"],
    ]


NOW = dt.datetime(2026, 11, 23, 18, 0, tzinfo=dt.UTC)
HEAD = "a" * 40


class Stuck:
    """A stuck-run scenario: gh's pr view and run list, plus the run helpers."""

    def __init__(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = world
        self.statuses = ["in_progress", "completed"]
        self.conclusion = "cancelled"
        self.numbers: list[int] = [45]
        self.jobs: list[dict[str, str]] = []
        self.new_head = HEAD
        self.invoked: list[list[str]] = []
        self.pauses: list[float] = []
        self.meta: dict[str, object] = {
            "headRefOid": HEAD,
            "headRefName": "topic",
            "isCrossRepository": False,
        }
        self.runs: list[dict[str, object]] = []
        monkeypatch.setattr(pr_rerun, "utcnow", Mock(return_value=NOW))
        monkeypatch.setattr(pr_rerun, "run_view", self.view)
        monkeypatch.setattr(pr_github, "head_of", self.head)
        monkeypatch.setattr(pr_github, "invoke_gh", self.invoke)
        monkeypatch.setattr(pr_rerun, "pause", self.pauses.append)

    def head(self, _number: int) -> str:
        return self.new_head

    def view(self, _run_id: object, field: str) -> dict[str, object]:
        if field == "jobs":
            return {"jobs": self.jobs}
        return {"status": self.statuses.pop(0), "conclusion": self.conclusion}

    def invoke(self, argv: list[str], _body: str) -> tuple[int, str]:
        self.invoked.append(argv[1:])
        return (1 if argv[1:3] == ["run", "cancel"] and self.cancel_fails else 0), ""

    cancel_fails = False

    def start(self) -> None:
        self.world.fake(
            "gh",
            cases={
                "pr view": {"rc": 0, "stdout": json.dumps(self.meta), "stderr": ""},
                "run list": gh_list(self.runs),
                "api": {"rc": 0, "stdout": json.dumps(self.numbers), "stderr": ""},
            },
        )


def old_job(
    minutes: int, name: str = "bazel", status: str = "in_progress"
) -> dict[str, str]:
    started = NOW - dt.timedelta(minutes=minutes)
    return {"name": name, "status": status, "startedAt": started.isoformat()}


def a_run(run_id: int = 7, **fields: object) -> dict[str, object]:
    run_fields: dict[str, object] = {
        "databaseId": run_id,
        "status": "in_progress",
        "headSha": HEAD,
    }
    return run_fields | fields


def test_rerun_stuck_cancels_waits_and_reruns(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(61)]
    scene.statuses = ["in_progress", "in_progress", "completed"]
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 0
    assert scene.invoked == [["run", "cancel", "7"], ["run", "rerun", "7"]]
    assert scene.pauses == [pr_rerun.CANCEL_PAUSE, pr_rerun.CANCEL_PAUSE]
    argvs = [cast("list[str]", c["argv"]) for c in world.calls("gh")]
    list_call = next(argv for argv in argvs if argv[1] == "list")
    assert list_call[2:8] == [
        "--commit",
        HEAD,
        "--event",
        "pull_request",
        "--branch",
        "topic",
    ]


@pytest.mark.parametrize(
    ("runs", "jobs"),
    [
        ([], [old_job(300)]),
        ([a_run(status="completed")], [old_job(300)]),
        ([a_run(headSha="b" * 40)], [old_job(300)]),
        ([a_run()], [old_job(59)]),
        ([a_run()], [old_job(300, status="queued")]),
    ],
)
def test_rerun_stuck_leaves_a_run_that_is_not_stuck(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runs: list[dict[str, object]],
    jobs: list[dict[str, str]],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = runs
    scene.jobs = jobs
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert capsys.readouterr().err == "no stuck run for the current head of PR 45\n"
    assert scene.invoked == []


def test_rerun_stuck_ignores_a_run_of_another_pull_request(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.numbers = [46]
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert capsys.readouterr().err == "no stuck run for the current head of PR 45\n"
    assert scene.invoked == []


def test_rerun_stuck_skips_a_run_shared_with_another_pull_request(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.numbers = [46, 45]
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert capsys.readouterr().err == (
        "run 7 also belongs to PR 46; skipping\n"
        "no stuck run for the current head of PR 45\n"
    )
    assert scene.invoked == []


def test_rerun_stuck_does_not_rerun_a_run_that_completed_normally(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.conclusion = "success"
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert "completed as success; no rerun" in capsys.readouterr().err
    assert scene.invoked == [["run", "cancel", "7"]]


def test_rerun_stuck_skips_a_young_run_to_reach_a_stuck_one(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run(9), a_run(7)]
    ages = {9: 5, 7: 90}

    def view(run_id: object, field: str) -> dict[str, object]:
        if field == "jobs":
            return {"jobs": [old_job(ages[int(str(run_id))])]}
        return {"status": "completed", "conclusion": "cancelled"}

    monkeypatch.setattr(pr_rerun, "run_view", view)
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 0
    assert scene.invoked == [["run", "cancel", "7"], ["run", "rerun", "7"]]


def test_rerun_stuck_refuses_a_fork(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.meta["isCrossRepository"] = True
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert "comes from a fork" in capsys.readouterr().err
    assert len(world.calls("gh")) == 1


def test_rerun_stuck_refuses_when_the_head_moved(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.new_head = "c" * 40
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert "moved to a new head" in capsys.readouterr().err
    assert scene.invoked == []


def test_rerun_stuck_stops_when_the_cancel_fails(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.cancel_fails = True
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert scene.invoked == [["run", "cancel", "7"]]


def test_rerun_stuck_refuses_when_the_cancel_never_completes(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.statuses = ["in_progress"] * pr_rerun.CANCEL_POLLS
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert "did not finish cancelling" in capsys.readouterr().err
    assert scene.invoked == [["run", "cancel", "7"]]
    assert len(scene.pauses) == pr_rerun.CANCEL_POLLS


def test_rerun_stuck_reports_an_unreadable_pull_request(world: World) -> None:
    world.fake("gh", rc=1, stderr="gone")
    with pytest.raises(SystemExit, match="cannot read PR 45: gone"):
        run_main("rerun", "45", "--stuck")


def test_run_view_reports_an_unreadable_run(world: World) -> None:
    world.fake("gh", rc=1, stderr="gone")
    with pytest.raises(SystemExit, match="cannot read run 7: gone"):
        pr_rerun.run_view(7, "jobs")


def test_run_view_returns_the_mapping_gh_prints(world: World) -> None:
    world.fake("gh", stdout='{"jobs": []}')
    assert pr_rerun.run_view(7, "jobs") == {"jobs": []}


def test_pause_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = Mock()
    monkeypatch.setattr(pr_rerun.time, "sleep", slept)
    pr_rerun.pause(2.5)
    slept.assert_called_once_with(2.5)


def test_utcnow_is_timezone_aware() -> None:
    assert pr_rerun.utcnow().tzinfo is dt.UTC


def test_rerun_reports_a_missing_pull_request(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake(
        "gh",
        rc=1,
        stderr="Could not resolve to a PullRequest with the number of 45\n",
    )
    with pytest.raises(SystemExit) as info:
        run_main("rerun", "45")
    assert str(info.value) == (
        "cannot read the head of PR 45: "
        "Could not resolve to a PullRequest with the number of 45"
    )
    assert capsys.readouterr().out == ""
    assert len(world.calls("gh")) == 1


@pytest.mark.parametrize("argv", [("rerun",), ("rerun", "abc")])
def test_rerun_requires_a_numeric_pr_number(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main(*argv)
    assert info.value.code == 2
