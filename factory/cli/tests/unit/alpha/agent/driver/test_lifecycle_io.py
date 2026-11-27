"""Exercise coordinator lifecycle through fake tools and run records."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from driver_scenarios import (
    CONFIG,
    command,
    kept,
    launch,
    set_clock,
    ticking,
)

from tbhbdev.alpha.agent import model, transport
from tbhbdev.alpha.agent.decisions import remote as remote_commands
from tbhbdev.alpha.agent.driver import (
    lifecycle,
    metadata,
)
from tbhbdev.alpha.agent.driver.decisions import output

if TYPE_CHECKING:
    from agent_driver_example import Remote


@pytest.mark.parametrize("verb", ["status", "wait", "unreachable", "finished"])
def test_unsandboxed_run_is_visible(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    if verb == "finished":
        remote.configure(launch={"rc": "0"})
    assert launch(remote, "--account", "ops", "--unsandboxed", "o/r#1") == 0
    suffix = " sandbox=none authorized_by=o/r#1"
    assert suffix in capsys.readouterr().out
    if verb == "finished":
        return
    if verb == "wait":
        remote.file(".rc", "0")
        remote.configure(session=False)
    if verb == "unreachable":
        remote.configure(ssh=[{"rc": 255}] * 10)
        ticking(monkeypatch, 240)
    assert command("wait" if verb == "unreachable" else verb) == (
        75 if verb == "unreachable" else 0
    )
    assert suffix in capsys.readouterr().out


@pytest.mark.parametrize("final", [True, False])
def test_wait_success(
    remote: Remote, capsys: pytest.CaptureFixture[str], final: bool
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.file(".rc", "4\n")
    remote.file(".out", "stream answer\n")
    if final:
        remote.file(".out.final", "final answer\n")
    expected = kept()
    assert command("wait") == 4
    assert capsys.readouterr() == (
        "sample: rc=4\n"
        + ("final answer\n" if final else "stream answer\n")
        + "\n"
        + output.INSPECTION_WARNING
        + "\n"
        + expected,
        "",
    )


def test_wait_unreachable(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(ssh=[{"rc": 255}] * 4)
    ticking(monkeypatch)
    assert command("wait") == 75
    assert capsys.readouterr() == (
        "sample: rc=unreachable\n" + output.INSPECTION_WARNING + "\n",
        "",
    )


def test_wait_unknown(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(session=False)
    expected = kept()
    assert command("wait") == 1
    assert capsys.readouterr() == (
        "sample: rc=unknown\n\n" + output.INSPECTION_WARNING + "\n" + expected,
        "",
    )


@pytest.mark.parametrize(
    ("session", "rc", "expected"),
    [
        (True, None, "running"),
        (False, "0\n", "0"),
        (False, "7\n", "7"),
        (False, None, "unknown"),
    ],
)
def test_status(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    session: bool,
    rc: str | None,
    expected: str,
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(session=session)
    remote.file(".phase", "running\n")
    if rc is not None:
        remote.file(".rc", rc)
    before = len(remote.records("ssh"))
    assert command("status") == 0
    assert len(remote.records("ssh")) == before + 1
    output = capsys.readouterr()
    assert output.err == ""
    started = json.loads(metadata.metadata_path("sample").read_text())["started"]
    assert output.out == (
        f"sample: phase=running rc={expected} host=example-linux "
        f"account=agent started={started} launcher=tools\n"
    )


def test_status_reports_an_unreachable_host_with_the_local_record(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()

    def unreachable(*_args: object, **_kwargs: object) -> transport.Reading:
        message = "example-linux: transport failed"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "read", unreachable)
    assert command("status") == 1
    output = capsys.readouterr()
    started = json.loads(metadata.metadata_path("sample").read_text())["started"]
    assert output.err == ""
    assert output.out == (
        f"sample: phase=unknown rc=unknown host=example-linux "
        f"account=agent started={started} launcher=tools"
        " host unreachable, run state unknown\n"
    )


@pytest.mark.parametrize(
    ("rc", "reason"),
    [
        (4, "push failed: rejected"),
        (4, "push refused: the draft is committed"),
        (5, "pr not opened: no draft"),
        (4, "commit check failed: cannot check new commits (TimeoutExpired)"),
        (6, "no new commit: the build required one (--expect-commit)"),
        (9, "push failed: no new tip for agent/sample on origin"),
        (10, "review not posted: the pull request head moved since the run judged it"),
    ],
)
def test_status_shows_the_last_publication_reason(
    remote: Remote, capsys: pytest.CaptureFixture[str], rc: int, reason: str
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.file(".phase", "finished\n")
    remote.file(".rc", f"{rc}\n")
    remote.file(
        ".log",
        "ordinary log text\n" * 4200 + f"push skipped: earlier\n{reason}\ncleanup\n",
    )
    assert command("status") == 0
    output = capsys.readouterr().out
    assert output.startswith(f"sample: phase=finished rc={rc} ({reason}) ")
    assert "earlier" not in output
    assert "cleanup" not in output


def test_kill_host_rc(remote: Remote) -> None:
    assert launch(remote) == 0
    remote.configure(kill_rc="143\n")
    assert command("kill") == 0
    assert (remote.state / "sample.rc").read_text() == "143\n"
    assert not (remote.state / "sample.rc.pending").exists()
    assert not any(
        ".rc.fallback" in str(record["args"]) for record in remote.records("ssh")
    )


def test_kill_fallback(remote: Remote, monkeypatch: pytest.MonkeyPatch) -> None:
    assert launch(remote) == 0
    ticking(monkeypatch)
    assert command("kill") == 0
    assert (remote.state / "sample.rc").read_text() == "137\n"
    assert (remote.state / "sample.rc").stat().st_mode & 0o777 == 0o600


def test_wait_retries_and_resets_failure_time(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(
        ssh=[
            {"rc": 255},
            {"out": "running\n\n1\n1\n"},
            {"rc": 255},
            {"out": "finished\n0\n1\n0\nanswer\n"},
        ]
    )
    ticking(monkeypatch, 40)
    expected = kept()
    assert command("wait") == 0
    assert capsys.readouterr() == (
        "sample: rc=0\nanswer\n\n" + output.INSPECTION_WARNING + "\n" + expected,
        "",
    )


def test_kill_refuses_live_session(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    remote.configure(ssh=[{"out": ""}, {"out": "running\n\n1\n1\n"}])
    ticking(monkeypatch)
    assert command("kill") == 1
    assert (
        "session still exists; return code left to run-host" in capsys.readouterr().err
    )
    assert not (remote.state / "sample.rc").exists()


def test_kill_waits_for_host_rc(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    remote.configure(
        ssh=[None, {"out": "stopping\n\n1\n0\n"}, {"out": "finished\n143\n1\n0\n"}]
    )
    ticking(monkeypatch, 1)
    assert command("kill") == 0
    assert not any(
        ".rc.fallback" in str(record["args"]) for record in remote.records("ssh")
    )


def test_wait_poll_and_transport_budget(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    ticks = iter(range(0, 1000, 110))
    sleeps: list[float] = []
    set_clock(monkeypatch, lambda: next(ticks), sleeps.append)
    readings = iter(
        [
            None,
            transport.Reading("running", None, launched=True, session=True, text=""),
            None,
            transport.Reading(
                "finished", 0, launched=True, session=False, text="answer\n"
            ),
        ]
    )
    timeouts: list[float] = []

    def read(
        config: model.Settings,
        host: model.Host,
        name: tuple[str, str],
        *,
        answer: bool,
        timeout: float,
    ) -> transport.Reading:
        assert config == CONFIG
        assert host == CONFIG.hosts[0]
        assert name == ("sample", metadata.load(CONFIG, "sample").owner)
        assert answer is True
        timeouts.append(timeout)
        reading = next(readings)
        if reading is None:
            message = "example"
            raise transport.TransportError(message)
        return reading

    monkeypatch.setattr(transport, "read", read)
    expected = kept()
    assert command("wait") == 0
    assert timeouts == [30, 20, 30, 20]
    assert sleeps == [30] * 3
    assert capsys.readouterr() == (
        "sample: rc=0\nanswer\n\n" + output.INSPECTION_WARNING + "\n" + expected,
        "",
    )


def test_wait_defaults_and_failure_deadline(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    ticks = iter([0, 0, 240, 240])
    sleeps: list[float] = []
    monkeypatch.setattr(
        lifecycle,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append),
    )
    timeouts: list[float] = []

    def read(
        _config: model.Settings,
        _host: model.Host,
        _name: tuple[str, str],
        *,
        answer: bool,
        timeout: float,
    ) -> transport.Reading:
        assert answer is True
        timeouts.append(timeout)
        message = "example"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "read", read)
    assert command("wait") == 75
    assert capsys.readouterr() == (
        "sample: rc=unreachable\n" + output.INSPECTION_WARNING + "\n",
        "",
    )
    assert sleeps == [30]
    assert timeouts == [30, 0]


@pytest.mark.parametrize("grace", [10, 80])
def test_kill_grace_and_poll_bounds(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, grace: int
) -> None:
    assert launch(remote) == 0
    config = dataclasses.replace(CONFIG, kill_grace_seconds=grace)
    run = metadata.load(config, "sample")
    ticks = iter([0, 0, grace + 1, grace + 1, grace + 2])
    sleeps: list[float] = []
    monkeypatch.setattr(
        lifecycle,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append),
    )
    scripts: list[str] = []
    timeouts: list[float] = []

    def shell(
        settings: model.Settings,
        host: model.Host,
        script: str,
        stdin: transport.Stdin | None = None,
    ) -> str:
        assert settings == config
        assert host == run.host
        assert stdin == transport.Stdin(run.owner)
        scripts.append(script)
        return ""

    def read(
        settings: model.Settings,
        host: model.Host,
        name: tuple[str, str],
        *,
        timeout: float,
    ) -> transport.Reading:
        assert settings == config
        assert host == run.host
        assert name == ("sample", metadata.load(CONFIG, "sample").owner)
        timeouts.append(timeout)
        return transport.Reading(
            "stopping", None, launched=True, session=False, text=""
        )

    monkeypatch.setattr(transport, "shell", shell)
    monkeypatch.setattr(transport, "read", read)
    assert lifecycle.kill(config, run) == 0
    assert timeouts == [min(30, grace + 2), 1]
    assert sleeps == [0.5]
    assert scripts == [
        remote_commands.session("sample", "kill-session", CONFIG.sockets_dir),
        remote_commands.fallback("sample", CONFIG.sockets_dir),
    ]


def test_wait_tmux_error_expires_without_rc(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    remote.configure(tmux_error="unrelated failure")
    ticking(monkeypatch, 240)
    assert command("wait") == 75
    assert not (remote.state / "sample.rc").exists()


def test_kill_ignores_stale_pending(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    remote.file(".rc.pending", "old partial result")
    ticking(monkeypatch)
    assert command("kill") == 0
    assert (remote.state / "sample.rc").read_text() == "137\n"
    assert (remote.state / "sample.rc.fallback").is_file()


def test_inspection_warning(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(session=False)
    remote.file(".rc", "0")
    assert command("wait") == 0
    assert (
        "Don't run git inside a run clone: it is agent-written. "
        "Use `tbhbdev alpha agent log|diff NAME`." in capsys.readouterr().out
    )


def test_wait_keeps_an_unsandboxed_success(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--account", "ops", "--unsandboxed", "o/r#1") == 0
    capsys.readouterr()
    remote.configure(session=False)
    remote.file(".rc", "0\n")
    expected = kept()
    assert command("wait") == 0
    assert expected in capsys.readouterr().out
    assert metadata.metadata_path("sample").exists()
