"""Exercise coordinator dispatch through fake tools and run records."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_cli_example import run_agent
from driver_scenarios import CONFIG, command, launch

from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent import signing_parity, transport
from tbhbdev.alpha.agent.driver import metadata

if TYPE_CHECKING:
    from agent_driver_example import Remote


def test_signing_parity_dispatches_with_loaded_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parity = Mock(return_value=1)
    monkeypatch.setattr(agent_settings, "load", lambda: CONFIG)
    monkeypatch.setattr(signing_parity, "main", parity)
    assert run_agent(["signing-parity"]) == 1
    parity.assert_called_once_with(CONFIG)


def test_send_refuses_unknown_run(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command("send", "hello") == 1
    assert "sample: unknown run" in capsys.readouterr().err
    assert remote.records("ssh") == []


@pytest.mark.usefixtures("isolated_home")
def test_send_explicit_claude_needs_no_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert command("send", "--harness", "claude", "hello") == 1
    assert capsys.readouterr().err == "Claude delivery is not built\n"


@pytest.mark.parametrize("verb", ["status", "wait", "keep", "kill", "log", "diff"])
def test_verbs_without_remote_run_directory(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.state.rename(remote.root / "saved-run-files")
    capsys.readouterr()
    assert command(verb) == 1
    assert "not owner" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()
    assert not remote.state.exists()


@pytest.mark.usefixtures("remote")
def test_status_without_local_run_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = metadata.metadata_path("sample")
    path.parent.mkdir(parents=True)
    assert command("status") == 1
    assert str(path) in capsys.readouterr().err


@pytest.mark.parametrize(
    "verb", ["kill", "wait", "clean", "status", "keep", "log", "diff"]
)
def test_failed_transfer_cannot_control_other_owner(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        remote.file(".owner", "another-owner\n")
        remote.file(".rc", "0\n")
        message = "transfer failed"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "stream", fail)
    assert launch(remote) == 1
    assert not metadata.metadata_path("sample").exists()
    archived = next((metadata.directory() / "trash").glob("*/sample.json"))
    metadata.metadata_path("sample").write_bytes(archived.read_bytes())
    before = {p.name: p.read_bytes() for p in remote.state.iterdir()}
    capsys.readouterr()
    assert command(verb) == 1
    assert "not owner" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()
    assert {p.name: p.read_bytes() for p in remote.state.iterdir()} == before
    assert not remote.records("tmux")


@pytest.mark.parametrize("verb", ["status", "kill", "clean", "log", "diff"])
def test_unsandboxed_run_verbs(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    assert launch(remote, "--account", "ops", "--unsandboxed", "o/r#1") == 0
    capsys.readouterr()
    if verb in {"kill", "clean"}:
        remote.configure(session=False)
        remote.file(".rc", "0")
    if verb in {"log", "diff"}:
        remote.file(".out.final", "verdict\n")
    assert command(verb, *(["--force"] if verb == "clean" else [])) == 0
    output = capsys.readouterr()
    if verb == "status":
        assert "sandbox=none authorized_by=o/r#1" in output.out
    if verb == "clean":
        assert not metadata.metadata_path("sample").exists()
    if verb == "kill":
        assert any("kill-session" in str(call) for call in remote.records("tmux"))
