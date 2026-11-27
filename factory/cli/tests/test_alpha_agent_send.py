"""Exercise steering with a fake app-server proxy."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import app_server, host_tools

if TYPE_CHECKING:
    from pathlib import Path

THREAD = '{"type":"thread.started","thread_id":"thread"}\n'
INACTIVE = "Codex thread has no active turn"
MESSAGE = "next\u00a0line\nthen this"


@pytest.fixture
def live_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log = tmp_path / "tbhbdev/agent-runs/sample.out"
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


@pytest.mark.usefixtures("live_log")
@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize(
    "response",
    [
        ({"data": [{"status": "inProgress", "id": "active"}]}, ""),
        ({"data": [{"status": "completed"}]}, INACTIVE),
        ({"data": []}, INACTIVE),
        ({}, "thread not loaded"),
        (None, app_server.UNLOADED),
        (False, app_server.UNLOADED),
        (True, "Codex app-server timed out"),
    ],
)
def test_send_host_proxy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: tuple[dict[str, object] | bool | None, str],
    platform: str,
    tmp_path: Path,
) -> None:
    payload, error = response
    local_bin = tmp_path / ".npm-global" / "bin"
    local_bin.mkdir(parents=True)
    codex = local_bin / "codex"
    codex.touch(mode=0o700)
    path = f"{tmp_path}/first:{tmp_path}/second"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", path)
    monkeypatch.setattr(sys, "platform", platform)
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    frame = Mock(wraps=app_server.frame)
    monkeypatch.setattr(app_server, "frame", frame)

    async def connect(
        *command: str, env: dict[str, str] | None = None, **_options: int
    ) -> SimpleNamespace:
        assert command[1:4] == ("app-server", "proxy", "--sock")
        assert command[0] == str(codex)
        assert env is not None
        assert env["PATH"].startswith(f"{path}:")
        assert {str(local_bin), "/usr/local/bin"} <= set(env["PATH"].split(":"))
        if payload is True:
            raise TimeoutError
        reader = asyncio.StreamReader()
        status = b"403 Forbidden" if payload is False else b"101 Switching Protocols"
        reader.feed_data(b"HTTP/1.1 " + status + b"\r\n\r\n")
        reader.feed_data(reply({"id": 1, "result": {}}, 128))
        reader.feed_data(reply({"method": "turn/started"}))
        if payload:
            reader.feed_data(reply({"id": 2, "result": payload}, 66000))
        elif payload == {}:
            reader.feed_data(reply({"id": 2, "error": {"message": error}}))
        else:
            reader.feed_data(b"\x88\x00")
        reader.feed_data(reply({"id": 3, "result": {}}))
        reader.feed_eof()
        return SimpleNamespace(
            stdin=writer, stdout=reader, kill=Mock(), communicate=AsyncMock()
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", connect)
    assert agent.main(["send-host", "sample"], "example agent") == 0
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


@pytest.mark.usefixtures("live_log")
def test_send_host_requires_codex(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(host_tools, "account_path", Mock(return_value=(str(tmp_path),)))
    assert app_server.main(["sample"], "send-host") == 0
    assert capsys.readouterr().out.strip() == app_server.UNLOADED


@pytest.mark.parametrize("contents", [None, "", '{"type":"other"}\n'])
def test_send_host_requires_thread(
    live_log: Path,
    capsys: pytest.CaptureFixture[str],
    contents: str | None,
) -> None:
    if contents is None:
        live_log.unlink()
    else:
        live_log.write_text(contents)
    assert app_server.main(["sample"], "send-host") == 0
    assert capsys.readouterr().out.strip() == app_server.UNLOADED


def test_frame_supports_long_messages() -> None:
    assert app_server.frame(b"x" * 65536)[:10] == b"\x81\xff" + (65536).to_bytes(
        8, "big"
    )
