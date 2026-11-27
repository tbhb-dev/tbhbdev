# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for how pull request creation handles responses from gh."""

import json
from typing import cast

import pytest
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_test_support import (
    World,
    ledgers,
    run_main,
    write_draft,
)

host_names = pr_test_support.host_names
world = pr_test_support.world


@pytest.mark.parametrize("dry_run", [False, True])
def test_create_refuses_an_open_pr(
    world: World, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    world.fake(
        "gh",
        cases={
            "pr view --json number,state,url": {
                "rc": 0,
                "stdout": json.dumps(
                    {
                        "number": 17,
                        "state": "OPEN",
                        "url": "https://github.com/tbhb-dev/example/pull/17",
                    }
                ),
                "stderr": "",
            }
        },
    )
    write_draft(world)
    assert run_main("create", *(["--dry-run"] if dry_run else [])) == 1
    assert capsys.readouterr() == (
        "",
        (
            "error: PR 17 (https://github.com/tbhb-dev/example/pull/17) "
            "is already open; "
            "run `tbhbdev alpha pr edit DRAFT --pr 17` to apply the draft\n"
        ),
    )
    assert [call["argv"] for call in world.calls("gh")] == [
        ["pr", "view", "--json", "number,state,url"]
    ]


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_create_after_a_finished_pr(world: World, state: str) -> None:
    world.fake(
        "gh",
        stdout="https://github.com/tbhb-dev/example/pull/18\n",
        cases={
            "pr view --json number,state,url": {
                "rc": 0,
                "stdout": json.dumps({"number": 17, "state": state, "url": "old"}),
                "stderr": "",
            }
        },
    )
    write_draft(world)
    assert run_main("create") == 0
    view, create = world.calls("gh")
    assert view["stdin"] == ""
    assert cast("list[str]", create["argv"])[:2] == ["pr", "create"]


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr"),
    [
        (1, "", "authentication failed"),
        (0, "not JSON", ""),
        (0, "[]", ""),
        (0, "{}", ""),
        *[
            (
                0,
                json.dumps({"state": "OPEN", "number": 17, "url": "old", key: value}),
                "",
            )
            for key in ("state", "number", "url")
            for value in (None, [1], {"value": 1}, True, 1.5)
        ],
        *[
            (
                0,
                json.dumps(
                    {
                        key: value
                        for key, value in {
                            "state": "OPEN",
                            "number": 17,
                            "url": "old",
                        }.items()
                        if key != missing
                    }
                ),
                "",
            )
            for missing in ("state", "number", "url")
        ],
        *[
            (
                0,
                json.dumps({"state": "OPEN", "number": 17, "url": "old", key: value}),
                "",
            )
            for key, value in (("state", 17), ("number", "17"), ("url", 17))
        ],
        (1, "", "authentication failed\n\n"),
        (1, "", "authentication failed \n"),
        (1, "", "authentication error X\n"),
        (0, "", "no pull requests found"),
    ],
)
def test_create_stops_when_the_lookup_fails(
    world: World, capsys: pytest.CaptureFixture[str], rc: int, stdout: str, stderr: str
) -> None:
    world.fake(
        "gh",
        cases={
            "pr view --json number,state,url": {
                "rc": rc,
                "stdout": stdout,
                "stderr": stderr,
            }
        },
    )
    write_draft(world)
    assert run_main("create") == 1
    assert capsys.readouterr() == (
        "",
        f"cannot check for an open PR: {(stderr or stdout).rstrip(chr(10))}\n",
    )
    assert len(world.calls("gh")) == 1


@pytest.mark.parametrize(
    "result", [(rc, ending) for rc in (0, 1) for ending in ("", "\n", "\n\n")]
)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("url", ["", "https://github.com/tbhb-dev/example/pull/17"])
def test_create_rejects_already_exists(
    world: World,
    capsys: pytest.CaptureFixture[str],
    result: tuple[int, str],
    stream: str,
    url: str,
) -> None:
    rc, ending = result
    output = f"a pull request Already Exists {url}{ending}"
    answer: dict[str, object] = {"rc": rc, "stdout": "", "stderr": "", stream: output}
    world.fake("gh", cases={"pr create": answer})
    write_draft(world)
    assert run_main("create") == 1
    number = "17" if url else "<n>"
    assert capsys.readouterr() == (
        "",
        output.rstrip("\n")
        + "\n"
        + (
            f"gh pr create failed; run `tbhbdev alpha pr edit DRAFT --pr {number}` "
            "to apply the draft\n"
        ),
    )
    assert not ledgers(world).exists()


def test_create_passes_success_stderr_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", stdout="created\n", stderr="warning\n")
    write_draft(world)
    assert run_main("create") == 0
    assert capsys.readouterr() == ("created\n", "warning\n")


@pytest.mark.parametrize("ending", ["", "\n", "\n\n"])
def test_create_joins_error_streams(
    world: World, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    world.fake(
        "gh",
        cases={
            "pr create": {
                "rc": 1,
                "stdout": "request failed" + ending,
                "stderr": "connection lost" + ending,
            }
        },
    )
    write_draft(world)
    assert run_main("create") == 1
    assert capsys.readouterr() == ("", "request failed\nconnection lost\n")


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("request failed\n", "\n\n", "request failed\n"),
        ("\n\n", "connection lost\n", "connection lost\n"),
        (
            "request failed \n",
            "connection lost \n",
            "request failed \nconnection lost \n",
        ),
        ("request X\n", "connection X\n", "request X\nconnection X\n"),
        ("", "", "\n"),
    ],
)
def test_create_normalizes_empty_error_streams(
    world: World,
    capsys: pytest.CaptureFixture[str],
    stdout: str,
    stderr: str,
    expected: str,
) -> None:
    world.fake("gh", cases={"pr create": {"rc": 1, "stdout": stdout, "stderr": stderr}})
    write_draft(world)
    assert run_main("create") == 1
    assert capsys.readouterr() == ("", expected)
