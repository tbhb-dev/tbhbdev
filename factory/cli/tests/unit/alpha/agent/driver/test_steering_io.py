"""Exercise coordinator steering through fake tools and run records."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest
from driver_scenarios import (
    CONFIG,
    command,
    launch,
)

from tbhbdev.alpha.agent import app_server
from tbhbdev.alpha.agent.driver import (
    metadata,
)

if TYPE_CHECKING:
    from agent_driver_example import Remote


@pytest.mark.parametrize(
    ("text", "reply", "error"),
    [
        ("one\ntwo", {}, ""),
        ("hello", {"out": app_server.UNLOADED}, "unknown or unloaded"),
        ("hello", {"rc": 73}, "not owner"),
        ("op://example/item/field", None, "1Password reference"),
    ],
)
def test_send_routes_message_over_owned_ssh(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    text: str,
    reply: dict[str, object] | None,
    error: str,
) -> None:
    remote.configure(steer=True)
    assert launch(remote, "--steer") == 0
    owner = metadata.load(CONFIG, "sample").owner
    calls = remote.records("ssh", limits=True)
    if reply is not None:
        remote.configure(ssh=[reply])
    assert command("send", text) == int(bool(error))
    assert error in capsys.readouterr().err
    if reply is None:
        assert remote.records("ssh", limits=True) == calls
    else:
        call = remote.records("ssh")[-1]
        assert "send-host sample" in str(call["args"])
        assert text not in str(call["args"])
        assert base64.b64decode(str(call["stdin"])) == f"{owner}\n{text}".encode()
