"""Exercise coordinator startup through fake tools and run records."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from driver_scenarios import (
    CONFIG,
    PREPARE,
    launch,
    set_clock,
)

from tbhbdev.alpha.agent import model, transport
from tbhbdev.alpha.agent.driver import launch as coordinator
from tbhbdev.alpha.agent.driver import (
    metadata,
    startup,
)

if TYPE_CHECKING:
    from agent_driver_example import Remote


@pytest.mark.parametrize("reason", ["policy refused", "host tainted"])
@pytest.mark.parametrize("full_access", [False, True])
def test_refused_launch_leaves_no_metadata(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    full_access: bool,
) -> None:
    if full_access:
        monkeypatch.setattr(coordinator, "prepare", PREPARE)
    remote.configure(launch={"rc": "1\n", "log": reason + "\n"})
    assert launch(remote) == 1
    assert capsys.readouterr() == (f"sample: rc=1\n{reason}\n", "")
    assert not metadata.metadata_path("sample").exists()
    assert not (remote.state / "sample.owner").exists()
    trash = next((remote.state / "trash").iterdir())
    assert (trash / "sample.log").read_text() == reason + "\n"

    remote.configure(launch={"launched": ""})
    assert launch(remote) == 0
    assert metadata.metadata_path("sample").exists()


@pytest.mark.parametrize(("rc", "launched"), [(0, False), (0, True), (7, True)])
def test_completed_launch_keeps_metadata(
    remote: Remote, rc: int, launched: bool
) -> None:
    records = {"rc": f"{rc}\n"}
    if launched:
        records["launched"] = ""
    remote.configure(launch=records)
    assert launch(remote) == rc
    assert metadata.metadata_path("sample").exists()
    assert (remote.state / "sample.owner").exists()


@pytest.mark.parametrize("rc", [0, 7])
def test_rc_first(remote: Remote, capsys: pytest.CaptureFixture[str], rc: int) -> None:
    remote.configure(launch={"rc": f"{rc}\n", "log": "setup stopped\n", "launched": ""})
    assert launch(remote) == rc
    assert capsys.readouterr() == (f"sample: rc={rc}\nsetup stopped\n", "")
    assert remote.records("tmux")[-1]["args"] == [
        "-S",
        str(remote.root / "sockets/agents"),
        "-f",
        "/dev/null",
        "kill-session",
        "-t",
        "=ag-sample",
    ]


def test_launch_deadline(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Leave time for the input calls, then cross the deadline during polling.
    ticks = iter([0, 0, 0, 0, 0, 0, 120, 120, 120, 134, 134, 134])
    set_clock(monkeypatch, lambda: next(ticks))
    remote.configure(launch={"phase": "preparing"}, kill_rc="124\n")
    assert launch(remote) == 1
    assert (
        "launch unconfirmed host=example-linux phase=preparing"
        in capsys.readouterr().out
    )
    assert (remote.state / "sample.rc").read_text() == "124\n"


def test_launch_polls_then_acknowledges(remote: Remote) -> None:
    remote.configure(ssh=[None, None, None, {"out": "preparing\n\n0\n1\n"}, None])
    assert launch(remote) == 0
    assert len(remote.records("ssh")) == 5


def test_launch_poll_interval_and_budget(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    run = metadata.load(CONFIG, "sample")
    ticks = iter([0, 1, 1, 2])
    sleeps: list[float] = []
    monkeypatch.setattr(
        startup,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append),
    )
    outcomes = iter([False, True])
    timeouts: list[float] = []

    def read(
        settings: model.Settings,
        host: model.Host,
        name: tuple[str, str],
        *,
        timeout: float,
    ) -> transport.Reading:
        assert settings == CONFIG
        assert host == run.host
        assert name == ("sample", metadata.load(CONFIG, "sample").owner)
        timeouts.append(timeout)
        return transport.Reading(
            "running", None, launched=next(outcomes), session=True, text=""
        )

    monkeypatch.setattr(transport, "read", read)
    assert startup.poll_launch(CONFIG, run, 120) == 0
    assert timeouts == [120, 119]
    assert sleeps == [0.5]


@pytest.mark.parametrize("launched", [True, False])
def test_shims_extend_launch_poll_budget(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, launched: bool
) -> None:
    assert launch(remote) == 0
    run = metadata.load(CONFIG, "sample")
    ticks = iter([119, 120, 121, 720])
    sleeps: list[float] = []
    monkeypatch.setattr(
        startup,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append),
    )
    phases = [
        transport.Reading("shims", None, launched=False, session=True, text=""),
        transport.Reading(
            "running" if launched else "shims",
            None,
            launched=launched,
            session=True,
            text="",
        ),
    ]
    read = Mock(side_effect=phases)
    unconfirmed = Mock(return_value=1)
    monkeypatch.setattr(transport, "read", read)
    monkeypatch.setattr(startup, "unconfirmed", unconfirmed)
    assert startup.poll_launch(CONFIG, run, 120) == (0 if launched else 1)
    assert [call.kwargs["timeout"] for call in read.call_args_list] == [1, 599]
    assert sleeps == [0.5]
    if launched:
        unconfirmed.assert_not_called()
    else:
        unconfirmed.assert_called_once_with(CONFIG, run, "shims")


def test_poll_transport_failure_only_kills_created_session(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    remote.configure(ssh=[None, None, None, {"rc": 255}], kill_rc="1\n")
    assert launch(remote) == 1
    assert capsys.readouterr().out == (
        "sample: launch unconfirmed host=example-linux phase=unreachable\n"
    )
    assert any("kill-session" in str(r) for r in remote.records("tmux"))


def test_owner_lost_during_launch_poll(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def read(*_args: object, **_kwargs: object) -> transport.Reading:
        message = "owner lost"
        raise transport.CollisionError(message)

    monkeypatch.setattr(transport, "read", read)
    assert launch(remote) == 1
    assert "not owner" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()


def test_owner_lost_during_unconfirmed_shutdown(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> transport.Reading:
        remote.file(".owner", "new-owner\n")
        message = "lost poll reply"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "read", fail)
    assert launch(remote) == 1
    assert "not owner" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()
    assert not any("kill-session" in str(r) for r in remote.records("tmux"))


@pytest.mark.parametrize("failure", ["transfer", "deadline"])
def test_unsandboxed_unconfirmed_reports_sandbox(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    if failure == "transfer":
        original = transport.call

        def lost_reply(
            config: model.Settings,
            host: model.Host,
            command: tuple[str, ...],
            stdin: transport.Stdin | None = None,
            *,
            timeout: float = 30,
        ) -> str:
            reply = original(config, host, command, stdin, timeout=timeout)
            if "new-session" in command[2]:
                message = "lost start reply"
                raise transport.TransportError(message)
            return reply

        monkeypatch.setattr(transport, "call", lost_reply)
    else:
        ticks = iter([0, 0, 0, 0, 0, 0, 120, 120, 120, 134, 134, 134])
        set_clock(monkeypatch, lambda: next(ticks))
        remote.configure(launch={"phase": "preparing"}, kill_rc="124\n")
    assert launch(remote, "--account", "ops", "--unsandboxed", "o/r#1") == (
        75 if failure == "transfer" else 1
    )
    phase = "unreachable" if failure == "transfer" else "preparing"
    assert capsys.readouterr().out == (
        f"sample: launch unconfirmed host=example-linux phase={phase}"
        " sandbox=none authorized_by=o/r#1\n"
    )
