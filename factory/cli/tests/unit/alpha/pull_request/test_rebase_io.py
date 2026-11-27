# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request rebase."""

import json
import re
import subprocess
from typing import cast
from unittest.mock import Mock

import pytest
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_draft_samples import (
    REBASE_BODY,
    REBASE_LINE,
    TICKED_REBASE_BODY,
    TICKED_REBASE_LINE,
)
from unit.alpha.pull_request.pr_test_support import (
    World,
    pr_page,
    ready_cases,
    run_main,
)

from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import repository as pr_repository
from tbhbdev.alpha.pull_request import tools as pr_tools

host_names = pr_test_support.host_names
world = pr_test_support.world

REBASE_LOGIN = "tbhb-dev-renovate[bot]"


def rebase_view(**fields: object) -> subprocess.CompletedProcess[bytes]:
    data = {"author": {"login": REBASE_LOGIN}, "state": "OPEN", "body": REBASE_BODY}
    return subprocess.CompletedProcess([], 0, json.dumps(data | fields).encode(), b"")


def fake_rebase_gh(
    monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess[bytes]
) -> tuple[Mock, Mock]:
    read = Mock(side_effect=responses)
    write = Mock(return_value=(0, ""))
    monkeypatch.setattr(
        pr_repository, "origin_repository", Mock(return_value="tbhb-dev/example")
    )
    monkeypatch.setattr(
        pr_tools, "find_tool", Mock(side_effect={"gh": "/bin/gh"}.__getitem__)
    )
    monkeypatch.setattr(pr_tools, "run_bytes", read)
    monkeypatch.setattr(pr_github, "invoke_gh", write)
    return read, write


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing", [True, False])
def test_rebase_ticks_only_the_renovate_checkbox(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ending: str,
    trailing: bool,
) -> None:
    body = (REBASE_BODY if trailing else REBASE_BODY.rstrip("\n")).replace("\n", ending)
    expected = body.replace(REBASE_LINE, TICKED_REBASE_LINE)
    read, write = fake_rebase_gh(monkeypatch, rebase_view(body=body))
    assert run_main("rebase", "8") == 0
    target = ["8", "--repo", "github.com/tbhb-dev/example"]
    read.assert_called_once_with(
        ["/bin/gh", "pr", "view", *target, "--json", "author,state,body"]
    )
    write.assert_called_once_with(
        ["gh", "pr", "edit", *target, "--body-file", "-"], expected, quiet=True
    )
    actual = cast("str", write.call_args.args[1]).encode()
    assert len(actual) == len(body.encode())
    assert sum(a != b for a, b in zip(actual, body.encode(), strict=True)) == 1
    assert capsys.readouterr() == ("PR 8: ticked the Renovate rebase checkbox\n", "")


def test_rebase_leaves_other_checkboxes_on_the_marker_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = REBASE_LINE + " (from `- [ ]`)"
    _, write = fake_rebase_gh(monkeypatch, rebase_view(body=body))
    assert run_main("rebase", "8") == 0
    assert write.call_args.args[1] == body.replace("- [ ]", "- [x]", 1)


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        (
            {"author": {"login": "app/tbhb-dev-agent-pull-requests"}},
            f"author is not {REBASE_LOGIN}",
        ),
        ({"author": {"login": REBASE_LOGIN.upper()}}, f"author is not {REBASE_LOGIN}"),
        ({"author": None}, f"author is not {REBASE_LOGIN}"),
        ({"state": "CLOSED"}, "pull request is not open"),
        ({"body": None}, "cannot read pull request body"),
        ({"body": "- [ ] unrelated checkbox\n"}, "no Renovate rebase checkbox line"),
        (
            {"body": f"{REBASE_LINE}\n{REBASE_LINE}"},
            "multiple Renovate rebase checkbox lines",
        ),
        ({"body": REBASE_LINE[6:]}, "malformed Renovate rebase checkbox line"),
    ],
)
def test_rebase_refuses_an_ineligible_pull_request(
    monkeypatch: pytest.MonkeyPatch, fields: dict[str, object], why: str
) -> None:
    read, write = fake_rebase_gh(monkeypatch, rebase_view(**fields))
    with pytest.raises(SystemExit, match=f"^PR 8: {re.escape(why)}$"):
        run_main("rebase", "8", "9")
    assert read.call_count == 1
    write.assert_not_called()


def test_rebase_refuses_when_pr_view_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    response = subprocess.CompletedProcess([], 1, rebase_view().stdout, b"failed")
    _, write = fake_rebase_gh(monkeypatch, response)
    with pytest.raises(SystemExit, match=r"^PR 8: cannot read pull request on origin$"):
        run_main("rebase", "8")
    write.assert_not_called()


@pytest.mark.parametrize(
    ("code", "login", "accepted"),
    [(0, REBASE_LOGIN, True), (1, REBASE_LOGIN, False), (0, "other[bot]", False)],
)
def test_rebase_verifies_the_app_login(
    monkeypatch: pytest.MonkeyPatch, code: int, login: str, accepted: bool
) -> None:
    view = rebase_view(author={"login": "app/tbhb-dev-renovate"})
    canonical = subprocess.CompletedProcess([], code, f"{login}\n".encode(), b"")
    read, write = fake_rebase_gh(monkeypatch, view, canonical)
    if accepted:
        assert run_main("rebase", "8") == 0
        assert write.call_count == 1
    else:
        with pytest.raises(
            SystemExit, match=f"^PR 8: author is not {re.escape(REBASE_LOGIN)}$"
        ):
            run_main("rebase", "8")
        write.assert_not_called()
    assert read.call_count == 2
    assert read.call_args.args == (
        [
            "/bin/gh",
            "api",
            "--hostname",
            "github.com",
            "repos/tbhb-dev/example/pulls/8",
            "--jq",
            ".user.login",
        ],
    )


def test_rebase_already_ticked_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ticked = rebase_view(body=TICKED_REBASE_BODY)
    _, write = fake_rebase_gh(monkeypatch, ticked, ticked)
    assert run_main("rebase", "8", "9") == 0
    write.assert_not_called()
    assert capsys.readouterr() == (
        (
            "PR 8: the Renovate rebase checkbox is already ticked\n"
            "PR 9: the Renovate rebase checkbox is already ticked\n"
        ),
        "",
    )


def test_rebase_stops_at_the_first_failed_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    read, _ = fake_rebase_gh(monkeypatch, rebase_view(), rebase_view())
    write = Mock(side_effect=[(0, ""), (3, "")])
    monkeypatch.setattr(pr_github, "invoke_gh", write)
    assert run_main("rebase", "8", "9", "10") == 3
    assert [call.args[0][3] for call in write.call_args_list] == ["8", "9"]
    assert [call.args[0][3] for call in read.call_args_list] == ["8", "9"]
    assert capsys.readouterr() == (
        "PR 8: ticked the Renovate rebase checkbox\n",
        "PR 9: could not tick the Renovate rebase checkbox\n",
    )


def test_rebase_prints_only_its_status(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    body = REBASE_BODY.rstrip("\n").replace("\n", "\r\n")
    cases = ready_cases()
    cases["pr view 8"] = {
        "rc": 0,
        "stdout": rebase_view(body=body).stdout.decode(),
        "stderr": "",
    }
    cases["pr edit 8"] = {
        "rc": 0,
        "stdout": pr_page("tbhb-dev/example", 8) + "\n",
        "stderr": "warning\n",
    }
    world.fake("gh", cases=cases)
    assert run_main("rebase", "8") == 0
    assert capsys.readouterr() == ("PR 8: ticked the Renovate rebase checkbox\n", "")
    assert world.calls("gh")[-1]["stdin"] == body.replace(
        REBASE_LINE, TICKED_REBASE_LINE
    )


@pytest.mark.parametrize("args", [(), ("abc",)])
def test_rebase_requires_numeric_pr_numbers(args: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main("rebase", *args)
    assert info.value.code == 2
