# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Send commands through the coordinator and a fake app-server proxy."""

from __future__ import annotations

import dataclasses
import io
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from agent_cli_example import run_agent
from agent_driver_example import execution_arguments, setup
from agent_example import MARKER, settings

from tbhbdev.alpha.agent import app_server, driver, model
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_home")


def test_send_refuses_a_saved_run_from_another_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")
    remote = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_settings, "load", lambda: config)
    assert run_agent(execution_arguments(remote.prompt)) == 0
    run = driver.load(config, "sample")
    run.request = dataclasses.replace(run.request, harness=model.Harness.AGY)
    with pytest.raises(Invalid, match="send requires a Codex run"):
        driver.send(config, run, "hello")


THREAD = '{"type":"thread.started","thread_id":"thread"}\n'


@pytest.mark.parametrize(
    ("log", "error"),
    [
        (None, app_server.UNLOADED),
        ("{}", app_server.UNLOADED),
        (THREAD, app_server.UNLOADED),
        (THREAD, "Codex app-server timed out"),
    ],
)
def test_send_host_fallback(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    log: str | None,
    error: str,
) -> None:
    path = isolated_home / ".xdg-state/tbhbdev/agent-runs/sample.out"
    if log is not None:
        path.parent.mkdir(parents=True)
        path.write_text(log)
    monkeypatch.setattr(sys, "stdin", io.StringIO("message"))
    monkeypatch.setattr(app_server.host_tools, "account_path", Mock(return_value=()))
    monkeypatch.setenv("PATH", str(isolated_home))
    if error != app_server.UNLOADED:
        monkeypatch.setattr(app_server, "proxy", AsyncMock(side_effect=TimeoutError))
    assert app_server.main(["sample"], "send-host") == 0
    assert capsys.readouterr().out.strip() == error
