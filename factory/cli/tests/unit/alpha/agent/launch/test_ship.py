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


def sample_run(digest: str | None = DIGEST) -> Run:
    run = Run("sample", LINUX, request.parse(BUILD), "", "owner-token")
    run.cli_sha256 = digest
    return run


@pytest.mark.parametrize("schema", [None, '{"type":"object"}'])
def test_send_streams_owner_inputs_and_bundle(
    monkeypatch: pytest.MonkeyPatch, schema: str | None
) -> None:
    cfg, run, bundle = settings(), sample_run(), b"packed-cli-bytes"
    inputs = Inputs('{"v":2}', "brief text\n", schema, run.owner)
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
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert (got_cfg, got_host, got_timeout) == (cfg, LINUX, 42.5)
    assert got_script == bootstrap.receive("sample", cfg.sockets_dir, DIGEST, python)
    assert got_stdin is not None
    owner_line, archive_bytes = got_stdin.encode().split(b"\n", 1)
    assert owner_line == b"owner-token"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
        contents = {
            info.name: (info.mode, handle.read())
            for info in archive.getmembers()
            if (handle := archive.extractfile(info)) is not None
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
    "error", [transport.TransportError("offline"), transport.CollisionError("taken")]
)
def test_send_surfaces_transport_failure(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    run = sample_run()
    inputs = Inputs("{}", "brief", None, run.owner)

    def fail(*_a: object, **_kw: object) -> str:
        raise error

    monkeypatch.setattr(transport, "shell", fail)
    with pytest.raises(type(error)) as caught:
        ship.send(settings(), LINUX, run, b"bundle", inputs, 1.0)
    assert caught.value is error


def test_send_requires_cli_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    run = sample_run(digest=None)
    calls: list[object] = []
    monkeypatch.setattr(transport, "shell", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(Invalid, match="cli_sha256"):
        ship.send(settings(), LINUX, run, b"bundle", Inputs("{}", "b", None, "o"), 1.0)
    assert calls == []
