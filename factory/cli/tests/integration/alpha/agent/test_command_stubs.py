"""The public CLI dispatches both bundle commands."""

from __future__ import annotations

import pytest
from agent_cli_example import run_agent


@pytest.mark.parametrize("verb", ["launch", "run"])
def test_bundle_command_refuses(verb: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_agent([verb]) == 1
    assert capsys.readouterr().err == f"tbhbdev alpha agent {verb}: not built\n"
