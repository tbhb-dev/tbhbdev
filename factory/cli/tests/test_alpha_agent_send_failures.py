"""Agent send failures through the CLI."""

from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from agent_cli_example import run_agent
from agent_driver_example import execution_arguments
from agent_example import settings

from tbhbdev.alpha.agent import app_server, arguments, driver, host_tools, transport
from tbhbdev.alpha.agent import settings as agent_settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.usefixtures("isolated_home")
def test_send_refuses_agy_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = settings()
    monkeypatch.setattr(agent_settings, "load", lambda: config)
    args = arguments.parser("agent").parse_args(
        execution_arguments("brief.md", "--harness", "agy", "--model", "gemini-example")
    )
    driver.save(driver.prepare(config, args))
    shell = Mock()
    monkeypatch.setattr(transport, "shell", shell)

    assert run_agent(["send", "sample", "hello"]) == 1
    assert capsys.readouterr().err == (
        "tbhbdev alpha agent: sample: send requires a Codex run\n"
    )
    shell.assert_not_called()


@pytest.fixture
def live_log(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = isolated_home / ".xdg-state/tbhbdev/agent-runs/sample.out"
    log.parent.mkdir(parents=True)
    log.write_text('{"type":"thread.started","thread_id":"thread"}\n')
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello"))
    return log


def test_send_host_without_thread_reports_unloaded(
    live_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live_log.write_text('{"type":"error"}\n')
    assert run_agent(["send-host", "sample"]) == 0
    assert capsys.readouterr().out.strip() == app_server.UNLOADED


@pytest.mark.usefixtures("live_log")
def test_send_host_without_codex_reports_unloaded(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PATH", str(isolated_home))
    monkeypatch.setattr(host_tools, "account_path", Mock(return_value=()))
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", connect)

    assert run_agent(["send-host", "sample"]) == 0
    assert capsys.readouterr().out.strip() == app_server.UNLOADED
    connect.assert_not_awaited()


@pytest.mark.usefixtures("live_log")
def test_send_host_reports_proxy_timeout(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex = isolated_home / "codex"
    codex.touch(mode=0o700)
    monkeypatch.setenv("PATH", str(isolated_home))
    timeout = asyncio.timeout

    def immediate_timeout(_seconds: float | None) -> asyncio.Timeout:
        return timeout(0)

    async def connect(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            stdout=asyncio.StreamReader(),
            kill=Mock(),
            communicate=AsyncMock(),
        )

    monkeypatch.setattr(asyncio, "timeout", immediate_timeout)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", connect)

    assert run_agent(["send-host", "sample"]) == 0
    assert capsys.readouterr().out.strip() == "Codex app-server timed out"
