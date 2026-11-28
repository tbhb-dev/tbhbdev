"""The launch entry point packs, saves, ships, starts, and polls."""

from __future__ import annotations

import json
from importlib import metadata as pkg
from typing import TYPE_CHECKING, NoReturn

import pytest
from agent_example import LINUX, settings

from tbhbdev.alpha.agent import cli_bundle, transport
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.driver import metadata, startup
from tbhbdev.alpha.agent.launch import command, ship
from tbhbdev.alpha.agent.launch.decisions import bootstrap
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tbhbdev.alpha.agent.driver.decisions.metadata import Run

PROG = "tbhbdev alpha agent launch"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "b" * 64
BUNDLE = b"packed-cli-archive"


def launch_argv(prompt: Path, *extra: str, profile: str = "build") -> list[str]:
    base = ("--host", LINUX.name, "--repo", "example-org/example-repo")
    opts = ("--model", "example-model", "--effort", "high", "--profile", profile)
    return [*base, *opts, *extra, "sample", str(prompt)]


def raising(error: Exception) -> Callable[..., NoReturn]:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    return fail


def forbid(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError


def noop(*_args: object, **_kwargs: object) -> str:
    return ""


def zero(*_args: object, **_kwargs: object) -> int:
    return 0


@pytest.fixture
def brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> Path:
    assert isolated_home.is_dir()
    path = tmp_path / "brief.md"
    path.write_text("Carry out the example.\n")
    monkeypatch.setattr(agent_settings, "load", settings)
    monkeypatch.setattr(cli_bundle, "installed", lambda: (BUNDLE, COMMIT, DIGEST))
    return path


def test_main_without_host_refuses(
    brief: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ship, "send", forbid)
    monkeypatch.setattr(transport, "call", forbid)
    with pytest.raises(Invalid, match="--host"):
        command.main(launch_argv(brief)[2:], PROG)
    assert not metadata.metadata_path("sample").exists()
    assert command.main(["--help"], PROG) == 0


@pytest.mark.parametrize(
    "error",
    [Invalid("bad commit"), OSError("unreadable"), pkg.PackageNotFoundError("tbhbdev")],
)
def test_pack_failure_refuses_before_saving_or_shipping(
    brief: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(cli_bundle, "installed", raising(error))
    monkeypatch.setattr(ship, "send", forbid)
    monkeypatch.setattr(transport, "call", forbid)
    with pytest.raises(Invalid, match=str(error)):
        command.main(launch_argv(brief), PROG)
    assert not metadata.metadata_path("sample").exists()


def test_successful_launch_saves_ships_starts_and_polls_in_order(
    brief: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, real_save = settings(), metadata.save
    events: list[str] = []
    recorded: dict[str, tuple[object, ...]] = {}

    def fake_save(run: Run) -> None:
        assert (run.cli_commit, run.cli_sha256) == (COMMIT, DIGEST)
        events.append("save")
        real_save(run)

    def fake_send(*args: object) -> None:
        recorded["ship"] = (*args, metadata.metadata_path("sample").exists())
        events.append("ship")

    def fake_call(*args: object, **_kwargs: object) -> str:
        recorded["start"] = args
        events.append("start")
        return ""

    def fake_poll(*_args: object) -> int:
        events.append("poll")
        return 0

    monkeypatch.setattr(agent_settings, "load", lambda: cfg)
    monkeypatch.setattr(metadata, "save", fake_save)
    monkeypatch.setattr(ship, "send", fake_send)
    monkeypatch.setattr(transport, "call", fake_call)
    monkeypatch.setattr(startup, "poll_launch", fake_poll)

    assert command.main(launch_argv(brief), PROG) == 0
    assert events == ["save", "ship", "start", "poll"]
    record = json.loads(metadata.metadata_path("sample").read_text())
    assert (record["cli_commit"], record["cli_sha256"]) == (COMMIT, DIGEST)
    got_cfg, got_host, _run, got_bundle, got_inputs, got_t, existed = recorded["ship"]
    assert (got_cfg, got_host, got_bundle, existed) == (cfg, LINUX, BUNDLE, True)
    assert isinstance(got_t, float)
    assert got_t > 0
    assert isinstance(got_inputs, transport.Inputs)
    assert got_inputs.schema is None
    owner = transport.Stdin(metadata.load(cfg, "sample").owner)
    start = bootstrap.start("sample", cfg.sockets_dir, LINUX.name)
    assert recorded["start"] == (cfg, LINUX, start, owner)


def test_launch_passes_schema_warnings_and_review_commit(
    brief: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief.write_text("Work on a branch named other-branch.\n")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}')
    shipped: list[transport.Inputs] = []

    def capture(*args: object) -> None:
        assert isinstance(args[4], transport.Inputs)
        shipped.append(args[4])

    monkeypatch.setattr(ship, "send", capture)
    monkeypatch.setattr(transport, "call", noop)
    monkeypatch.setattr(startup, "poll_launch", zero)
    argv = launch_argv(
        brief, "--ref", COMMIT, "--schema", str(schema), profile="review"
    )
    assert command.main(argv, PROG) == 0
    assert "warning:" in capsys.readouterr().err
    assert shipped[0].schema == '{"type":"object"}'
    assert json.loads(shipped[0].request)["commit"] == COMMIT


def test_transfer_and_poll_errors(
    brief: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    recovered: list[str] = []
    unconfirmed: list[tuple[str, str]] = []

    def rec(_settings: object, run: Run) -> None:
        recovered.append(run.name)

    def unc(_settings: object, run: Run, phase: str) -> int:
        unconfirmed.append((run.name, phase))
        return 1

    monkeypatch.setattr(ship, "send", raising(transport.CollisionError("taken")))
    with pytest.raises(Invalid, match="taken"):
        command.main(launch_argv(brief), PROG)
    assert not metadata.metadata_path("sample").exists()

    monkeypatch.setattr(ship, "send", raising(transport.TransportError("offline")))
    monkeypatch.setattr(startup, "recover_transfer", rec)
    assert command.main(launch_argv(brief), PROG) == 75
    assert recovered == ["sample"]
    assert "launch unconfirmed" in capsys.readouterr().out
    metadata.metadata_path("sample").unlink()

    monkeypatch.setattr(ship, "send", noop)
    monkeypatch.setattr(transport, "call", noop)
    monkeypatch.setattr(
        startup, "poll_launch", raising(transport.CollisionError("taken"))
    )
    with pytest.raises(Invalid, match="not owner"):
        command.main(launch_argv(brief), PROG)

    monkeypatch.setattr(
        startup, "poll_launch", raising(transport.TransportError("offline"))
    )
    monkeypatch.setattr(startup, "unconfirmed", unc)
    assert command.main(launch_argv(brief), PROG) == 1
    assert unconfirmed == [("sample", "unreachable")]
