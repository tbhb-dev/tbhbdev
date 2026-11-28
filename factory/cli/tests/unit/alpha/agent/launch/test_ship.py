"""Bundle transfer streams owner, inputs, and the CLI archive."""

from __future__ import annotations

import io
import sys
import tarfile

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
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_shell(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(transport, "shell", fake_shell)
    ship.send(cfg, LINUX, run, bundle, inputs, 42.5)

    assert len(calls) == 1
    args, kwargs = calls[0]
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    script = bootstrap.receive("sample", cfg.sockets_dir, DIGEST, py)
    assert args[:3] == (cfg, LINUX, script)
    assert kwargs == {"timeout": 42.5}
    assert isinstance(args[3], transport.Stdin)
    owner_line, raw = args[3].encode().split(b"\n", 1)
    assert owner_line == b"owner-token"
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
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
    def fail(*_a: object, **_kw: object) -> str:
        raise error

    monkeypatch.setattr(transport, "shell", fail)
    inputs = Inputs("{}", "b", None, "o")
    with pytest.raises(type(error)) as caught:
        ship.send(settings(), LINUX, sample_run(), b"b", inputs, 1.0)
    assert caught.value is error


def test_send_requires_cli_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(*_a: object, **_kw: object) -> str:
        raise AssertionError

    monkeypatch.setattr(transport, "shell", forbid)
    with pytest.raises(Invalid, match="cli_sha256"):
        ship.send(
            settings(), LINUX, sample_run(None), b"b", Inputs("{}", "b", None, "o"), 1.0
        )
