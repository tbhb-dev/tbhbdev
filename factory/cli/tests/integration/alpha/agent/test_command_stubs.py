"""The public CLI dispatches both bundle commands."""

from __future__ import annotations

from agent_cli_example import run_agent
lazy import pytest


def test_launch_command_requires_host(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_agent(["launch"]) == 2
    err = capsys.readouterr().err
    assert "tbhbdev alpha agent launch: error:" in err
    assert "--host" in err


def test_run_command_needs_a_run_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_agent(["run"]) == 1
    assert capsys.readouterr().err == "tbhbdev alpha agent run: run name is required\n"
