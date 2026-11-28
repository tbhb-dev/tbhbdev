"""Exercise coordinator dispatch through fake tools and run records."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_cli_example import run_agent
from driver_scenarios import CONFIG, command, launch, set_clock

from tbhbdev.alpha.agent import process_group, runfiles, signing_parity, transport
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.decisions.run_state import Group
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
    assert "unknown run; local record absent" in capsys.readouterr().err
    assert command("wait") == 1
    assert str(path) in capsys.readouterr().err


def _launching() -> runfiles.RunFiles:
    files = runfiles.RunFiles(metadata.directory(), "sample")
    files.write("request.json", "{}")
    files.path("run.lock").touch()
    return files


@pytest.mark.usefixtures("remote")
def test_launching_run_without_local_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = _launching()

    assert command("status") == 0
    assert "phase=launching" in capsys.readouterr().out
    assert command("kill") == 1
    assert "no process group recorded, lock not held" in capsys.readouterr().err
    with runfiles.locked(files.path("run.lock")):
        assert command("clean", "--force") == 1
        assert "run is running" in capsys.readouterr().err
        assert command("kill") == 1
        assert "timed out waiting" in capsys.readouterr().err
        set_clock(monkeypatch, lambda: 0, lambda _: files.write("rc", "1\n"))
        assert command("kill") == 0
        assert "already finished" in capsys.readouterr().out


def _setup_record() -> runfiles.RunFiles:
    """A launching run whose group record is the setup worker's."""
    files = _launching()
    files.write("pgid", '{"pgid": 1234, "boot": "current", "start": "123"}')
    files.write("phase", "shims")
    return files


@pytest.mark.usefixtures("remote")
def test_kill_waits_past_the_setup_record_for_the_harness_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = _setup_record()
    # The setup worker's group is gone; any use of its record would fail.

    identity = Mock(return_value=Group(4321, "current", "7"))
    stop = Mock(return_value=True)
    monkeypatch.setattr(process_group, "identity", identity)
    monkeypatch.setattr(process_group, "stop_owned", stop)
    ticks = iter(range(100))

    def harness_starts(_: float) -> None:
        files.write("pgid", '{"pgid": 4321, "boot": "current", "start": "7"}')
        files.write("launched", "\n")

    with runfiles.locked(files.path("run.lock")):
        set_clock(monkeypatch, lambda: next(ticks) / 10, harness_starts)
        assert command("kill") == 0
    assert capsys.readouterr().err == ""
    assert stop.call_args.args[1:] == (4321, 0)
    identity.assert_called_once_with(4321)


@pytest.mark.usefixtures("remote")
def test_kill_times_out_when_only_the_setup_record_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = _setup_record()
    ticks = iter(range(1000))

    with runfiles.locked(files.path("run.lock")):
        set_clock(monkeypatch, lambda: next(ticks) * 10)
        assert command("kill") == 1
    assert "timed out waiting for a process group" in capsys.readouterr().err


@pytest.mark.usefixtures("remote")
def test_running_run_without_local_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = runfiles.RunFiles(metadata.directory(), "sample")
    files.write("pgid", '{"pgid": 1234, "boot": "current", "start": "123"}')
    files.write("phase", "running")
    identity = Mock(
        side_effect=[Group(1234, "current", "456"), Group(1234, "current", "123")]
    )
    stop = Mock(return_value=True)
    monkeypatch.setattr(process_group, "identity", identity)
    monkeypatch.setattr(process_group, "stop_owned", stop)

    assert command("status") == 0
    status = capsys.readouterr().out
    assert "phase=running" in status
    assert "local record absent" in status
    assert command("kill") == 1
    assert "run group identity no longer matches" in capsys.readouterr().err
    stop.assert_not_called()

    assert command("kill") == 0
    identity.assert_called_with(1234)
    assert stop.call_args.args[0].name == "sample"
    assert stop.call_args.args[1:] == (1234, 0)
    monkeypatch.setattr(process_group, "alive", Mock(return_value=True))
    assert command("clean", "--force") == 1
    assert "run is running" in capsys.readouterr().err
    files.write("pgid", '{"pgid": 0, "boot": "current", "start": "123"}')
    stop.reset_mock()
    assert command("kill") == 1
    assert "invalid run group ID" in capsys.readouterr().err
    stop.assert_not_called()


@pytest.mark.usefixtures("remote")
def test_ended_run_without_local_record(capsys: pytest.CaptureFixture[str]) -> None:
    files = runfiles.RunFiles(metadata.directory(), "sample")
    files.write("rc", "4\n")
    files.write("audit.json", "{}")
    files.write("launched", "\n")
    files.write("log", "review finished\n")

    assert command("status") == 0
    assert "phase=ended rc=4" in capsys.readouterr().out
    assert command("kill") == 0
    assert "already finished" in capsys.readouterr().out
    files.write("keep", "")
    assert command("status") == 0
    assert "phase=kept rc=4" in capsys.readouterr().out
    assert command("clean") == 1
    files.path("keep").unlink()
    trash = files.root / "trash"
    trash.symlink_to(files.root)
    assert command("clean", "--force") == 1
    assert "not a plain directory" in capsys.readouterr().err
    trash.unlink()
    assert command("clean", "--force") == 0
    output = capsys.readouterr().out
    for suffix in ("audit.json", "launched", "log", "rc"):
        assert f"sample.{suffix}" in output
    assert not list(files.root.glob("sample.*"))
    assert command("clean", "--force") == 0
    assert capsys.readouterr().out == ""


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
