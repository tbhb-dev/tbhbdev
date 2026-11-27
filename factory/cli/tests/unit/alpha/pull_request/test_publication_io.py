# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for how pull request creation handles the result of gh pr create."""

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
