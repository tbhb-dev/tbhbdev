# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request ready."""

import pytest
from fake_tool import PR_ORIGIN as ORIGIN
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_test_support import (
    World,
    gh_argvs,
    ready_cases,
    run_main,
)

host_names = pr_test_support.host_names
world = pr_test_support.world

READY_VIEW = "pr view 45 --repo github.com/tbhb-dev/example --json author --jq"
READY_ME = "api --hostname github.com user --jq .login"
READY_REPO = "github.com/tbhb-dev/example"


def test_ready_marks_the_pull_request_ready(world: World) -> None:
    world.fake("gh", cases=ready_cases())
    assert run_main("ready", "45") == 0
    assert gh_argvs(world) == [
        ["repo", "view", ORIGIN, "--json", "nameWithOwner,url"],
        [
            *["pr", "view", "45", "--repo", READY_REPO],
            "--json",
            "author",
            "--jq",
            ".author.login",
        ],
        ["api", "--hostname", "github.com", "user", "--jq", ".login"],
        ["pr", "ready", "45", "--repo", READY_REPO],
    ]
    assert [c["stdin"] for c in world.calls("gh")] == ["", "", ""]


def test_ready_accepts_a_pull_request_the_driver_opened(world: World) -> None:
    world.fake("gh", cases=ready_cases(author="app/tbhb-dev-agent-pull-requests"))
    assert run_main("ready", "45") == 0
    assert gh_argvs(world)[-1] == ["pr", "ready", "45", "--repo", READY_REPO]


@pytest.mark.parametrize(
    ("author", "me"),
    [
        ("human", "agent"),
        ("app/some-bot", "agent"),
        ("app/", "agent"),
        ("null", "agent"),
        ("Agent", "agent"),
        (" agent", "agent"),
        ("agent ", "agent"),
        ("", ""),
        (" ", " "),
        ("agent\r", "agent"),
        ("agent", "agent\r"),
    ],
)
def test_ready_refuses_a_login_that_is_not_the_agents(
    world: World, author: str, me: str
) -> None:
    cases = ready_cases(author=author, me=me)
    world.fake("gh", cases=cases)
    with pytest.raises(SystemExit, match=r"isn't this agent's account$"):
        run_main("ready", "45")
    assert not any(a[:2] == ["pr", "ready"] for a in gh_argvs(world))


@pytest.mark.parametrize("failing", [READY_VIEW, READY_ME])
def test_ready_refuses_when_a_login_is_unreadable(world: World, failing: str) -> None:
    cases = ready_cases()
    cases[failing] = {"rc": 1, "stdout": "agent\n", "stderr": "bad\n token"}
    world.fake("gh", cases=cases)
    with pytest.raises(SystemExit, match=r"isn't this agent's account: bad token$"):
        run_main("ready", "45")
    assert not any(a[:2] == ["pr", "ready"] for a in gh_argvs(world))


@pytest.mark.parametrize(
    ("stderr", "shown"),
    [
        ("see https://example.test/x?token=abc now", "see <redacted> now"),
        (
            "bad " + "gh" + "p_" + "0123456789abcdefABCDEF" + " token",
            "bad <redacted> token",
        ),
        ("a" * 19, "a" * 19),
        ("z" * 20, "<redacted>"),
        ("w " * 150, "w " * 100),
    ],
)
def test_ready_refusal_redacts_and_bounds_gh_stderr(
    world: World, stderr: str, shown: str
) -> None:
    cases = ready_cases()
    cases[READY_VIEW] = {"rc": 1, "stdout": "agent\n", "stderr": stderr}
    world.fake("gh", cases=cases)
    with pytest.raises(SystemExit) as info:
        run_main("ready", "45")
    message = str(info.value)
    assert message.endswith(f"account: {shown}")


def test_ready_acts_on_origin_even_when_gh_defaults_elsewhere(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The author read and the ready call name origin, not GH_REPO or GH_HOST."""
    monkeypatch.setenv("GH_REPO", "other.example/someone/else")
    monkeypatch.setenv("GH_HOST", "other.example")
    world.fake("gh", cases=ready_cases())
    assert run_main("ready", "45") == 0
    pinned = [a for a in gh_argvs(world) if a[:2] in (["pr", "view"], ["pr", "ready"])]
    assert len(pinned) == 2
    assert all(a[a.index("--repo") + 1] == READY_REPO for a in pinned)


def test_ready_marks_each_pull_request_in_order(world: World) -> None:
    cases = ready_cases()
    cases[READY_VIEW.replace("view 45", "view 46")] = cases[READY_VIEW]
    world.fake("gh", cases=cases)
    assert run_main("ready", "45", "46") == 0
    marked = [a[2] for a in gh_argvs(world) if a[:2] == ["pr", "ready"]]
    assert marked == ["45", "46"]


def test_ready_stops_at_the_first_failure(world: World) -> None:
    cases = ready_cases()
    cases["pr ready"] = {"rc": 3, "stdout": "", "stderr": ""}
    world.fake("gh", cases=cases)
    assert run_main("ready", "45", "46") == 3
    assert [a[2] for a in gh_argvs(world) if a[:2] == ["pr", "ready"]] == ["45"]
    assert not any(a[:3] == ["pr", "view", "46"] for a in gh_argvs(world))


def test_ready_passes_gh_output_and_exit_code_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = ready_cases()
    cases["pr ready"] = {"rc": 3, "stdout": "out\n", "stderr": "err\n"}
    world.fake("gh", cases=cases)
    assert run_main("ready", "45") == 3
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("out\n", "err\n")


def test_ready_requires_a_pr_number() -> None:
    with pytest.raises(SystemExit) as info:
        run_main("ready")
    assert info.value.code == 2


def test_ready_rejects_a_pr_that_is_not_a_number() -> None:
    with pytest.raises(SystemExit) as info:
        run_main("ready", "abc")
    assert info.value.code == 2
