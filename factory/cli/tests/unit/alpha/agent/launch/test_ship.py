"""Bundle transfer streams owner, inputs, and the CLI archive."""

from __future__ import annotations

import io
import sys
import tarfile
from typing import TYPE_CHECKING

import pytest
from agent_example import LINUX, settings
from test_alpha_agent_request import BUILD

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.driver.decisions.metadata import Run
from tbhbdev.alpha.agent.launch import ship
from tbhbdev.alpha.agent.launch.decisions import bootstrap
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.agent.transport import Inputs

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, Settings

DIGEST = "a" * 64


@pytest.mark.parametrize("schema", [None, '{"type":"object"}'])
def test_send_streams_owner_inputs_and_bundle(
    monkeypatch: pytest.MonkeyPatch, schema: str | None
) -> None:
    cfg = settings()
    run = Run("sample", LINUX, request.parse(BUILD), "", "owner-token")
    run.cli_sha256 = DIGEST
    inputs = Inputs('{"v":2}', "brief text\n", schema, run.owner)
    bundle = b"packed-cli-bytes"
    calls: list[tuple[Settings, Host, str, transport.Stdin | None, float]] = []

    def fake_shell(
        config: Settings,
        host: Host,
        script: str,
        stdin: transport.Stdin | None = None,
        *,
        timeout: float = 30,
    ) -> str:
        calls.append((config, host, script, stdin, timeout))
        return ""

    monkeypatch.setattr(transport, "shell", fake_shell)

    ship.send(cfg, LINUX, run, bundle, inputs, 42.5)

    assert len(calls) == 1
    got_cfg, got_host, got_script, got_stdin, got_timeout = calls[0]
    assert got_cfg == cfg
    assert got_host == LINUX
    assert got_timeout == 42.5
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert got_script == bootstrap.receive("sample", cfg.sockets_dir, DIGEST, python)
    assert got_stdin is not None
    owner_line, archive_bytes = got_stdin.encode().split(b"\n", 1)
    assert owner_line == b"owner-token"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
        members = {
            info.name: (info.mode, archive.extractfile(info))
            for info in archive.getmembers()
        }
        contents = {
            name: (mode, handle.read() if handle is not None else b"")
            for name, (mode, handle) in members.items()
        }
    expected = {
        "sample.request.json": (0o600, b'{"v":2}'),
        "sample.prompt": (0o600, b"brief text\n"),
        "sample.cli.tar": (0o600, bundle),
    }
    if schema is not None:
        expected["sample.schema.json"] = (0o600, schema.encode())
    assert contents == expected


@pytest.mark.parametrize(
    "error",
    [
        transport.TransportError("host unreachable"),
        transport.CollisionError("name taken"),
    ],
)
def test_send_surfaces_transport_failure(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    run = Run("sample", LINUX, request.parse(BUILD), "", "owner-token")
    run.cli_sha256 = DIGEST
    inputs = Inputs("{}", "brief", None, run.owner)

    def fail(*_args: object, **_kwargs: object) -> str:
        raise error

    monkeypatch.setattr(transport, "shell", fail)

    with pytest.raises(type(error)) as caught:
        ship.send(settings(), LINUX, run, b"bundle", inputs, 1.0)
    assert caught.value is error


def test_send_requires_cli_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run("sample", LINUX, request.parse(BUILD), "", "owner-token")
    inputs = Inputs("{}", "brief", None, run.owner)
    called = False

    def unexpected(*_args: object, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(transport, "shell", unexpected)

    with pytest.raises(Invalid, match="cli_sha256"):
        ship.send(settings(), LINUX, run, b"bundle", inputs, 1.0)
    assert not called
