# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise the state hook's session rule and sign-off handling."""

import io
import json
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.guard import state

if TYPE_CHECKING:
    from pathlib import Path

TRAILER_MESSAGE = (
    "state-guard: agent commits carry no Signed-off-by or Co-authored-by "
    "trailer and no -s or --signoff. Remove it.\n"
)


type Result = tuple[int, str, str]


type Runner = Callable[[object], Result]


@pytest.fixture
def run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Runner:
    def call(data: object) -> Result:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        status = state.main()
        out = capsys.readouterr()
        return status, out.out, out.err

    return call


def bash(command: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


@pytest.mark.parametrize("text", [None, "", "[other]\nkey = 1\n"])
def test_the_commit_check_runs_without_config(
    config_file: Path, run: Runner, text: str | None
) -> None:
    config_file.unlink(missing_ok=True)
    if text is not None:
        config_file.write_text(text)
    assert run(bash("git commit -s -m x")) == (2, "", TRAILER_MESSAGE)


def test_an_invalid_config_allows_and_says_why(config_file: Path, run: Runner) -> None:
    config_file.write_text('[harness]\nrule = ""\n')
    status, out, err = run({"hook_event_name": "SessionStart"})
    assert (status, out) == (0, "")
    assert err.startswith("state-guard: ignoring the config: ")
    assert "[harness] rule must be a non-empty string" in err


def test_a_crash_allows(run: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_data: object) -> int:
        raise RuntimeError

    monkeypatch.setattr(state, "run", boom)
    assert run(bash("git status")) == (0, "", "")


def test_session_start_prints_the_rule(config_file: Path, run: Runner) -> None:
    config_file.write_text('[harness]\nrule = "Example rule: claims carry evidence."\n')
    assert run({"hook_event_name": "SessionStart"}) == (
        0,
        "Example rule: claims carry evidence.\n",
        "",
    )


def test_session_start_without_a_rule_prints_nothing(
    config_file: Path, run: Runner
) -> None:
    config_file.write_text("[harness]\n")
    assert run({"hook_event_name": "SessionStart"}) == (0, "", "")


def test_other_tools_and_odd_inputs_pass(run: Runner) -> None:
    for data in (
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": dict[str, object](),
        },
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": None},
        dict[str, object](),
    ):
        assert run(data) == (0, "", "")


def test_the_signoff_check_and_session_start_run_outside_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run(bash("git commit -s")) == (2, "", TRAILER_MESSAGE)
    assert run({"hook_event_name": "SessionStart"}) == (0, "", "")
