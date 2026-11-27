"""The agent CLI refuses a run name before contacting the selected host."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agent_cli_example import run_agent
from agent_driver_example import execution_arguments, setup
from agent_example import settings

from tbhbdev.alpha.agent import settings as agent_settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.usefixtures("isolated_home")
def test_exec_refuses_a_name_that_contains_a_configured_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_settings, "load", settings)
    args = execution_arguments(remote.prompt)
    args[-2] = "prepare-EXAMPLE-MACOS"

    assert run_agent(args) == 1
    assert remote.records("ssh") == []
    assert (
        capsys.readouterr().err
        == "tbhbdev alpha agent: run name contains a host name\n"
    )
