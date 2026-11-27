"""Exercise coordinator arguments through fake tools and run records."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agent_cli_example import run_agent
from driver_scenarios import (
    arguments,
    launch,
)

if TYPE_CHECKING:
    from agent_driver_example import Remote


def test_exec_help_describes_expected_commits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_agent(["exec", "--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert (
        "--expect-commit, --no-expect-commit require a new commit and a push"
        in help_text
    )


def test_exec_help_lists_model_examples(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert run_agent(["exec", "--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--harness {codex,claude,agy} harness to launch: Codex, Claude or agy" in (
        help_text
    )
    assert "--allow-non-gemini let agy run a non-Gemini model ID" in help_text
    assert "gemini-" in help_text
    assert "claude-opus-5-5" in help_text


@pytest.mark.parametrize("flag", ["--repo", "--model", "--effort", "--profile"])
def test_required_flags(remote: Remote, flag: str) -> None:
    args = arguments(remote)
    index = args.index(flag)
    del args[index : index + 2]
    assert run_agent(args) == 2
    assert not remote.records("ssh")


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--harness", "bad"), ("--profile", "bad"), ("--timeout", "bad")],
)
def test_parser_rejects_invalid_choices_before_ssh(
    remote: Remote, flag: str, value: str
) -> None:
    assert launch(remote, flag, value) == 2
    assert remote.records("ssh") == []


@pytest.mark.parametrize(
    "args",
    [
        ["diff", "sample", "-n", "1"],
        ["log", "sample", "--patch"],
        ["log", "sample", "-n", "invalid"],
    ],
)
def test_inspection_rejects_extra_options(args: list[str]) -> None:
    assert run_agent(args) == 2
