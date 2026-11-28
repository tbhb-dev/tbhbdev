"""The launch entry point packs, saves, ships, starts, and polls."""

from __future__ import annotations

import json
from importlib import metadata as importlib_metadata
from typing import TYPE_CHECKING

import pytest
from agent_example import LINUX, settings

from tbhbdev.alpha.agent import cli_bundle, transport
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.driver import metadata, startup
from tbhbdev.alpha.agent.launch import command, ship
from tbhbdev.alpha.agent.launch.decisions import bootstrap
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Host, Settings

PROG = "tbhbdev alpha agent launch"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "b" * 64
BUNDLE = b"packed-cli-archive"


def launch_argv(prompt: Path, *extra: str, include_host: bool = True) -> list[str]:
    host = ["--host", LINUX.name] if include_host else []
    return [
        *host,
        "--repo",
        "example-org/example-repo",
        "--model",
        "example-model",
        "--effort",
        "high",
        "--profile",
        "build",
        *extra,
        "sample",
        str(prompt),
    ]


@pytest.mark.usefixtures("isolated_home")
def test_main_without_host_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "brief.md"
    prompt.write_text("Carry out the example.\n")
    called = False

    def unexpected(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(ship, "send", unexpected)
    monkeypatch.setattr(transport, "call", unexpected)

    assert command.main(launch_argv(prompt, include_host=False), PROG) == 2
    assert "--host" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()
    assert not called


@pytest.mark.usefixtures("isolated_home")
@pytest.mark.parametrize(
    "error",
    [
        Invalid("CLI bundle requires a 40-hex build commit"),
        OSError("bundle unreadable"),
        importlib_metadata.PackageNotFoundError("tbhbdev"),
    ],
)
def test_pack_failure_refuses_before_saving_or_shipping(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    prompt = tmp_path / "brief.md"
    prompt.write_text("Carry out the example.\n")
    monkeypatch.setattr(agent_settings, "load", settings)

    def broken() -> tuple[bytes, str, str]:
        raise error

    called = False

    def unexpected(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(cli_bundle, "installed", broken)
    monkeypatch.setattr(ship, "send", unexpected)
    monkeypatch.setattr(transport, "call", unexpected)

    assert command.main(launch_argv(prompt), PROG) == 1
    assert str(error) in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()
    assert not called


@pytest.mark.usefixtures("isolated_home")
def test_successful_launch_saves_ships_starts_and_polls_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = settings()
    prompt = tmp_path / "brief.md"
    prompt.write_text("Carry out the example.\n")
    events: list[str] = []
    real_save = metadata.save

    def fake_save(run: Run) -> None:
        assert run.cli_commit == COMMIT
        assert run.cli_sha256 == DIGEST
        events.append("save")
        real_save(run)

    def fake_send(  # noqa: PLR0913, PLR0917 -- mirrors ship.send
        config: Settings,
        host: Host,
        run: Run,
        bundle: bytes,
        inputs: transport.Inputs,
        timeout: float,
    ) -> None:
        assert metadata.metadata_path("sample").exists()
        assert config == cfg
        assert host == LINUX
        assert run.cli_commit == COMMIT
        assert run.cli_sha256 == DIGEST
        assert bundle == BUNDLE
        assert inputs.owner == run.owner
        assert inputs.prompt == "Carry out the example.\n"
        assert inputs.schema is None
        assert timeout > 0
        events.append("ship")

    def fake_call(
        config: Settings,
        host: Host,
        remote: tuple[str, ...],
        stdin: transport.Stdin | None = None,
        *,
        timeout: float = 30,
    ) -> str:
        assert config == cfg
        assert host == LINUX
        assert remote == bootstrap.start("sample", cfg.sockets_dir, LINUX.name)
        assert stdin == transport.Stdin(metadata.load(cfg, "sample").owner)
        assert timeout > 0
        events.append("start")
        return ""

    def fake_poll(config: Settings, run: Run, deadline: float) -> int:
        assert config == cfg
        assert run.name == "sample"
        assert deadline > 0
        events.append("poll")
        return 0

    monkeypatch.setattr(agent_settings, "load", lambda: cfg)
    monkeypatch.setattr(cli_bundle, "installed", lambda: (BUNDLE, COMMIT, DIGEST))
    monkeypatch.setattr(metadata, "save", fake_save)
    monkeypatch.setattr(ship, "send", fake_send)
    monkeypatch.setattr(transport, "call", fake_call)
    monkeypatch.setattr(startup, "poll_launch", fake_poll)

    assert command.main(launch_argv(prompt), PROG) == 0
    assert events == ["save", "ship", "start", "poll"]
    record = json.loads(metadata.metadata_path("sample").read_text())
    assert record["cli_commit"] == COMMIT
    assert record["cli_sha256"] == DIGEST


@pytest.mark.usefixtures("isolated_home")
def test_launch_passes_schema_warnings_and_review_commit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = settings()
    prompt = tmp_path / "brief.md"
    prompt.write_text("Work on a branch named other-branch.\n")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}')
    shipped: list[transport.Inputs] = []

    def fake_send(
        _config: Settings,
        _host: Host,
        _run: Run,
        _bundle: bytes,
        inputs: transport.Inputs,
        _timeout: float,
    ) -> None:
        shipped.append(inputs)

    monkeypatch.setattr(agent_settings, "load", lambda: cfg)
    monkeypatch.setattr(cli_bundle, "installed", lambda: (BUNDLE, COMMIT, DIGEST))
    monkeypatch.setattr(ship, "send", fake_send)
    monkeypatch.setattr(transport, "call", lambda *_a, **_kw: "")
    monkeypatch.setattr(startup, "poll_launch", lambda *_a, **_kw: 0)

    argv = [
        "--host",
        LINUX.name,
        "--repo",
        "example-org/example-repo",
        "--model",
        "example-model",
        "--effort",
        "high",
        "--profile",
        "review",
        "--ref",
        COMMIT,
        "--schema",
        str(schema),
        "sample",
        str(prompt),
    ]
    assert command.main(argv, PROG) == 0
    assert "warning:" in capsys.readouterr().err
    assert len(shipped) == 1
    assert shipped[0].schema == '{"type":"object"}'
    assert json.loads(shipped[0].request)["commit"] == COMMIT


@pytest.mark.usefixtures("isolated_home")
def test_transfer_collision_archives_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "brief.md"
    prompt.write_text("Carry out the example.\n")
    monkeypatch.setattr(agent_settings, "load", settings)
    monkeypatch.setattr(cli_bundle, "installed", lambda: (BUNDLE, COMMIT, DIGEST))

    def collide(*_args: object, **_kwargs: object) -> None:
        message = "name collision"
        raise transport.CollisionError(message)

    monkeypatch.setattr(ship, "send", collide)

    assert command.main(launch_argv(prompt), PROG) == 1
    assert "name collision" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()


@pytest.mark.usefixtures("isolated_home")
def test_transfer_error_recovers_and_returns_75(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "brief.md"
    prompt.write_text("Carry out the example.\n")
    recovered: list[str] = []
    monkeypatch.setattr(agent_settings, "load", settings)
    monkeypatch.setattr(cli_bundle, "installed", lambda: (BUNDLE, COMMIT, DIGEST))

    def fail(*_args: object, **_kwargs: object) -> None:
        message = "host unreachable"
        raise transport.TransportError(message)

    monkeypatch.setattr(ship, "send", fail)
    monkeypatch.setattr(
        startup, "recover_transfer", lambda _s, run: recovered.append(run.name)
    )

    assert command.main(launch_argv(prompt), PROG) == 75
    assert recovered == ["sample"]
    assert "launch unconfirmed" in capsys.readouterr().out
    assert metadata.metadata_path("sample").exists()


@pytest.mark.usefixtures("isolated_home")
def test_poll_collision_marks_not_owner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "brief.md"
    prompt.write_text("Carry out the example.\n")
    monkeypatch.setattr(agent_settings, "load", settings)
    monkeypatch.setattr(cli_bundle, "installed", lambda: (BUNDLE, COMMIT, DIGEST))
    monkeypatch.setattr(ship, "send", lambda *_a, **_kw: None)
    monkeypatch.setattr(transport, "call", lambda *_a, **_kw: "")

    def collide(*_args: object, **_kwargs: object) -> int:
        message = "name collision"
        raise transport.CollisionError(message)

    monkeypatch.setattr(startup, "poll_launch", collide)

    assert command.main(launch_argv(prompt), PROG) == 1
    assert "not owner" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()


@pytest.mark.usefixtures("isolated_home")
def test_poll_transport_error_marks_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "brief.md"
    prompt.write_text("Carry out the example.\n")
    unconfirmed: list[tuple[str, str]] = []
    monkeypatch.setattr(agent_settings, "load", settings)
    monkeypatch.setattr(cli_bundle, "installed", lambda: (BUNDLE, COMMIT, DIGEST))
    monkeypatch.setattr(ship, "send", lambda *_a, **_kw: None)
    monkeypatch.setattr(transport, "call", lambda *_a, **_kw: "")

    def fail(*_args: object, **_kwargs: object) -> int:
        message = "host unreachable"
        raise transport.TransportError(message)

    def mark(_settings: Settings, run: Run, phase: str) -> int:
        unconfirmed.append((run.name, phase))
        return 1

    monkeypatch.setattr(startup, "poll_launch", fail)
    monkeypatch.setattr(startup, "unconfirmed", mark)

    assert command.main(launch_argv(prompt), PROG) == 1
    assert unconfirmed == [("sample", "unreachable")]
