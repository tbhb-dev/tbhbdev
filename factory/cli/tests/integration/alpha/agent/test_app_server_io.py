# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Send commands through the coordinator and a fake app-server proxy."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent_cli_example import run_agent
from agent_driver_example import execution_arguments, setup
from agent_example import MARKER, settings

from tbhbdev.alpha.agent import app_server, driver, model
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.model import Invalid

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
INACTIVE = "Codex thread has no active turn"
MESSAGE = "next\u00a0line\nthen this"


def test_socket_path_fits_macos_with_long_run_name() -> None:
    root = Path("/Users/agent/.local/state/tbhbdev/agent-runs")
    assert len(os.fsencode(app_server.socket_path(root, "a" * 51))) < 104


@pytest.fixture
def live_log(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = isolated_home / ".xdg-state/tbhbdev/agent-runs/sample.out"
    log.parent.mkdir(parents=True)
    log.write_text(THREAD)
    monkeypatch.setattr(sys, "stdin", io.StringIO(MESSAGE))
    return log


def reply(payload: dict[str, object], size: int = 0) -> bytes:
    body = json.dumps(payload).encode().ljust(size)
    if len(body) < 126:
        return bytes((129, len(body))) + body
    width = 2 if len(body) < 65536 else 8
    kind = 126 if width == 2 else 127
    return bytes((129, kind)) + len(body).to_bytes(width, "big") + body


def turn_reply(*turns: dict[str, str], size: int = 0) -> bytes:
    return reply({"id": 2, "result": {"data": turns}}, size)


def stream(response: bytes) -> bytes:
    upgrade = b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
    ready = reply({"id": 1, "result": {}}) + reply({"method": "turn/started"})
    return upgrade + ready + response + reply({"id": 3, "result": {}})


@pytest.mark.usefixtures("live_log")
@pytest.mark.parametrize(
    ("wire", "error"),
    [
        (stream(turn_reply({"status": "inProgress", "id": "active"}, size=66000)), ""),
        (stream(turn_reply({"status": "completed"}, size=128)), INACTIVE),
        (stream(turn_reply()), INACTIVE),
        (
            stream(reply({"id": 2, "error": {"message": "thread not loaded"}})),
            "thread not loaded",
        ),
        (stream(b"\x88\x00"), app_server.UNLOADED),
        (b"HTTP/1.1 403 Forbidden\r\n\r\n", app_server.UNLOADED),
        (None, "Codex app-server timed out"),
    ],
)
def test_send_host_proxy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wire: bytes | None,
    error: str,
    isolated_home: Path,
) -> None:
    codex = isolated_home / ".npm-global" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.touch(mode=0o700)
    monkeypatch.setenv("PATH", str(isolated_home))
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    frame = Mock(wraps=app_server.frame)
    monkeypatch.setattr(app_server, "frame", frame)

    async def connect(
        *command: str, env: dict[str, str], **_options: int
    ) -> SimpleNamespace:
        assert command[:4] == (str(codex), "app-server", "proxy", "--sock")
        assert env["PATH"].startswith(f"{isolated_home}:")
        assert "/usr/local/bin" in env["PATH"].split(":")
        if wire is None:
            raise TimeoutError
        reader = asyncio.StreamReader()
        reader.feed_data(wire)
        reader.feed_eof()
        return SimpleNamespace(
            stdin=writer, stdout=reader, kill=Mock(), communicate=AsyncMock()
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", connect)
    assert run_agent(["send-host", "sample"]) == 0
    assert capsys.readouterr().out.strip() == error
    if not error:
        assert json.loads(frame.call_args.args[0]) == {
            "id": 3,
            "method": "turn/steer",
            "params": {
                "threadId": "thread",
                "expectedTurnId": "active",
                "input": [{"type": "text", "text": MESSAGE, "text_elements": []}],
            },
        }


@pytest.mark.parametrize("log", [None, "{}", THREAD])
def test_send_host_reports_unloaded(
    live_log: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    log: str | None,
) -> None:
    if log is None:
        live_log.unlink()
    else:
        live_log.write_text(log)
    monkeypatch.setattr(app_server.host_tools, "account_path", Mock(return_value=()))
    monkeypatch.setenv("PATH", str(live_log.parent))
    assert app_server.main(["sample"], "send-host") == 0
    assert capsys.readouterr().out.strip() == app_server.UNLOADED


def test_frame_supports_long_messages() -> None:
    assert app_server.frame(b"x" * 65536)[:10] == b"\x81\xff" + (65536).to_bytes(
        8, "big"
    )
