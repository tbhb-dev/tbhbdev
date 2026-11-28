"""Exercise coordinator host selection and admission against fake tools."""

from __future__ import annotations

import argparse
import dataclasses
import json
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_cli_example import run_agent
from agent_driver_example import Remote, setup
from agent_example import LINUX, MARKER, settings
from driver_scenarios import set_clock

from tbhbdev import version
from tbhbdev.alpha import version_check
from tbhbdev.alpha.agent import admission, transport
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.decisions import remote_host
from tbhbdev.alpha.agent.driver import hosts, launcher, metadata
from tbhbdev.alpha.agent.model import Host, Invalid, Profile, Settings

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107

CONFIG = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)

    def load() -> Settings:
        return CONFIG

    monkeypatch.setattr(agent_settings, "load", load)
    return result


def launch(host: Remote, *options: str, target: str | None = "example-linux") -> int:
    named = [] if target is None else ["--host", target]
    args = [
        "exec", *named, "--repo", "example-org/example-repo",
        "--model", "m", "--effort", "high", "--profile", "build", *options,
        "sample", str(host.prompt),
    ]  # fmt: skip
    return run_agent(args)


def test_a_busy_host_refuses_before_any_transfer(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits="9 4 1000 900 100 0 - -")
    assert launch(host) == 1
    assert "load 9 >= limit 4" in capsys.readouterr().err
    assert len(host.records("ssh", limits=True)) == 2
    assert host.records("ssh") == []
    assert not metadata.metadata_path("sample").exists()


@pytest.fixture
def host_pool(host: Remote, monkeypatch: pytest.MonkeyPatch) -> Remote:
    busy = dataclasses.replace(LINUX, auto_select=True, load_limit=24)
    active = dataclasses.replace(busy, name="example-active")
    idle = dataclasses.replace(busy, name="example-idle")
    laptop = dataclasses.replace(LINUX, name="example-laptop")
    mac = dataclasses.replace(CONFIG.hosts[1], auto_select=True)
    config = dataclasses.replace(CONFIG, hosts=(busy, active, idle, laptop, mac))
    monkeypatch.setattr(agent_settings, "load", lambda: config)
    host.configure(
        limits="1.5 32 33554432 16777216 100 0 - -",
        limits_by_host={
            busy.name: "24 32 33554432 16777216 100 0 - -",
            active.name: "4 32 33554432 16777216 100 0 - -",
            laptop.name: "0.1 8 33554432 16777216 100 0 - -",
            mac.name: "0.2 8 33554432 16777216 100 0 - -",
        },
    )
    return host


@pytest.mark.parametrize("swap_warning", [False, True])
def test_exec_without_host_uses_the_idle_linux_host(
    host_pool: Remote, capsys: pytest.CaptureFixture[str], swap_warning: bool
) -> None:
    if swap_warning:
        host_pool.configure(limits="1.5 32 33554432 16777216 100 90 0.4 -")
    assert launch(host_pool, target=None) == 0
    record = json.loads(metadata.metadata_path("sample").read_text())
    assert record["host"] == "example-idle"
    error = capsys.readouterr().err
    assert ("warning: example-idle: swap used 90% > 80%" in error) is swap_warning
    assert_pool_probes(host_pool)


@pytest.mark.parametrize(
    ("launch_wait", "delayed_probe", "probe_timeouts"),
    [
        (120, "slot", [30, 29]),
        (20, "slot", [5, 4]),
        (20, "limits at deadline", [5]),
        (20, "limits timeout", [5]),
        (20, "install lock", [5]),
    ],
)
def test_exec_preserves_preflight_time_after_a_slow_probe(
    host: Remote,
    monkeypatch: pytest.MonkeyPatch,
    launch_wait: int,
    delayed_probe: str,
    probe_timeouts: list[float],
) -> None:
    preferred = dataclasses.replace(LINUX, auto_select=True)
    slow = dataclasses.replace(preferred, name="example-slow")
    last = dataclasses.replace(preferred, name="example-last")
    install_lock = delayed_probe == "install lock"
    times_out = delayed_probe in {"limits timeout", "install lock"}
    candidates = (slow, preferred) if times_out else (preferred, slow, last)
    config = dataclasses.replace(
        CONFIG, hosts=candidates, launch_wait_seconds=launch_wait
    )
    monkeypatch.setattr(agent_settings, "load", lambda: config)
    host.configure(
        limits="1 4 12582912 6291456 100 0 - -",
        limits_by_host={preferred.name: "0.5 4 12582912 6291456 100 0 - -"},
    )
    elapsed = 0.0
    set_clock(monkeypatch, lambda: elapsed)
    if install_lock:
        current = f"tbhbdev 0 ({'a' * 40})"
        host.configure(version=current)
        monkeypatch.setattr(version_check, "latest", Mock(return_value="a" * 40))
        monkeypatch.setattr(version, "line", lambda: current)
    real_shell = transport.shell
    slow_timeouts: list[float] = []

    def shell(
        settings: Settings,
        target: Host,
        script: str,
        stdin: transport.Stdin | None = None,
        *,
        timeout: float = 30,
    ) -> str:
        nonlocal elapsed
        assert timeout > 0
        if install_lock and target == slow and script.endswith("--version"):
            return f"tbhbdev 0 ({'b' * 40})"
        if target == slow and "# tbhbdev-heal-probe" not in script:
            slow_timeouts.append(timeout)
            if times_out or script == remote_host.check_slot(slow, Profile.BUILD):
                elapsed += timeout
                message = "probe timed out"
                raise transport.TransportError(message)
            elapsed += launch_wait / 2 if delayed_probe == "limits at deadline" else 1
        if script == remote_host.installed(preferred):
            assert target == preferred
            assert timeout == launch_wait - elapsed
            assert timeout >= launch_wait / 2
        return real_shell(settings, target, script, stdin, timeout=timeout)

    monkeypatch.setattr(transport, "shell", shell)
    assert launch(host, target=None) == 0
    assert slow_timeouts == probe_timeouts
    assert metadata.load(config, "sample").host == preferred


@pytest.mark.parametrize("probe_rc", [1, 2, 73])
def test_exec_without_host_keeps_the_idle_host_when_its_slot_probe_fails(
    host_pool: Remote, probe_rc: int
) -> None:
    host_pool.configure(slot_rc_by_host={"example-idle": probe_rc})
    assert launch(host_pool, target=None) == 0
    record = json.loads(metadata.metadata_path("sample").read_text())
    assert record["host"] == "example-idle"
    assert_pool_probes(host_pool)


@pytest.mark.parametrize("active_full", [False, True])
def test_exec_without_host_skips_hosts_without_a_slot(
    host_pool: Remote, capsys: pytest.CaptureFixture[str], active_full: bool
) -> None:
    full = ["example-linux", "example-idle"]
    if active_full:
        full.append("example-active")
    host_pool.configure(
        limits_by_host={
            "example-linux": "22 32 33554432 16777216 100 0 - -",
            "example-active": "4 32 33554432 16777216 100 0 - -",
        },
        full_slot_hosts=full,
    )
    if active_full:
        assert launch(host_pool, target=None) == 1
        error = capsys.readouterr().err
        assert "no usable linux hosts for automatic selection" in error
        assert host_pool.records("ssh") == []
        assert not metadata.metadata_path("sample").exists()
    else:
        assert launch(host_pool, target=None) == 0
        record = json.loads(metadata.metadata_path("sample").read_text())
        assert record["host"] == "example-active"
    assert_pool_probes(host_pool)


def test_exec_refuses_a_busy_named_host_and_lists_alternatives(
    host_pool: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(host_pool) == 1
    error = capsys.readouterr().err
    assert "example-linux: load 24 >= limit 24" in error
    assert "available alternatives: example-idle, example-active\n" in error
    assert_pool_probes(host_pool)
    assert host_pool.records("ssh") == []
    assert not metadata.metadata_path("sample").exists()


def assert_pool_probes(host: Remote) -> None:
    probes: list[str] = []
    for record in host.records("ssh", limits=True):
        args = record["args"]
        assert isinstance(args, list)
        command: list[object] = args
        script = command[-1]
        assert isinstance(script, str)
        if "# tbhbdev-limits" in script:
            name = command[-2]
            assert isinstance(name, str)
            probes.append(name)
    assert probes == ["example-linux", "example-active", "example-idle"]


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ((), "no usable linux hosts for automatic selection"),
        (("--ignore-limits",), "--ignore-limits requires --host"),
    ],
)
def test_exec_without_host_refuses_before_ssh(
    host: Remote,
    capsys: pytest.CaptureFixture[str],
    options: tuple[str, ...],
    error: str,
) -> None:
    assert launch(host, *options, target=None) == 1
    assert error in capsys.readouterr().err
    assert host.records("ssh", limits=True) == []


def test_full_swap_with_low_pressure_launches_with_a_warning_end_to_end(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits="0.5 4 12582912 6291456 8191 8167 0.4 -")
    assert launch(host) == 0
    assert "warning: example-linux: swap used 100% > 80%" in capsys.readouterr().err
    host.configure(limits="0.5 4 12582912 6291456 8191 8167 - -")
    with pytest.raises(Invalid, match="swap used 100% > 80%"):
        hosts.check_limits(CONFIG, CONFIG.hosts[0], Profile.BUILD, ignore=False)


def test_full_swap_uses_swap_in_when_pressure_is_unknown(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    config_host = CONFIG.hosts[0]
    host.configure(limits="0.5 4 12582912 6291456 100 99 - 0")
    hosts.check_limits(CONFIG, config_host, Profile.BUILD, ignore=False)
    assert (
        capsys.readouterr().err == f"warning: {config_host.name}: swap used 99% > 80%\n"
    )
    host.configure(limits="0.5 4 12582912 6291456 100 99 - 5000")
    expected = (
        rf"^{config_host.name}: swap-in 5000 pages/s >= limit 1000; "
        r"swap used 99% > 80%; pass --ignore-limits to launch anyway$"
    )
    with pytest.raises(Invalid, match=expected):
        hosts.check_limits(CONFIG, config_host, Profile.BUILD, ignore=False)


def test_ignore_limits_launches_warns_and_is_recorded(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits="9 4 1000 900 100 0 - -")
    assert launch(host, "--ignore-limits") == 0
    assert "warning: example-linux: load 9 >= limit 4" in capsys.readouterr().err
    assert json.loads(metadata.metadata_path("sample").read_text())["ignore_limits"]
    assert metadata.load(CONFIG, "sample").ignore_limits


def test_a_failed_read_refuses(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits_rc=1)
    assert launch(host) == 1
    assert "cannot read host limits: example-linux: remote command" in (
        capsys.readouterr().err
    )
    host.configure(limits_rc=0, limits="garbage")
    assert launch(host) == 1
    assert "cannot read host limits: unparsable output" in capsys.readouterr().err


def test_a_quiet_host_launches_without_a_warning(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(host) == 0
    assert "warning" not in capsys.readouterr().err
    assert not json.loads(metadata.metadata_path("sample").read_text())["ignore_limits"]


def test_a_disabled_host_refuses_even_with_limit_overrides(
    host: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    disabled = dataclasses.replace(
        CONFIG.hosts[0], max_concurrent_runs=0, limits_warn_only=True
    )

    def load() -> Settings:
        return dataclasses.replace(CONFIG, hosts=(disabled, *CONFIG.hosts[1:]))

    monkeypatch.setattr(agent_settings, "load", load)
    assert launch(host, "--ignore-limits") == 1
    assert "max_concurrent_runs is 0" in capsys.readouterr().err
    assert host.records("ssh", limits=True) == []


def test_build_reservation_uses_available_memory(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits="0.5 4 12582912 6291455 0 0 - -")
    assert launch(host) == 1
    assert (
        "memory available 6291455 KiB < reservation 6144 MiB" in capsys.readouterr().err
    )
    assert host.records("ssh") == []


def test_review_uses_its_own_reservation_at_the_boundary(
    host: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = dataclasses.replace(CONFIG, review_memory_mib=1024)
    config_host = config.hosts[0]

    def load() -> Settings:
        return config

    def session(*_: object) -> tuple[str, None]:
        return "tools", None

    monkeypatch.setattr(agent_settings, "load", load)
    monkeypatch.setattr(launcher, "session", session)
    host.configure(limits="0.5 4 4194304 1048575 0 0 - -")
    with pytest.raises(Invalid, match="reservation 1024 MiB"):
        hosts.check_limits(config, config_host, Profile.REVIEW, ignore=False)
    host.configure(limits="0.5 4 4194304 1048576 0 0 - -")
    assert launch(host, "--profile", "review", "--ref", "a" * 40) == 0
    with pytest.raises(Invalid, match="reservation 6144 MiB"):
        hosts.check_limits(config, config_host, Profile.BUILD, ignore=False)


def test_check_limits_runs_without_a_launch(host: Remote) -> None:
    host.configure(limits="9 4 1000 900 100 0 - -")
    config_host = CONFIG.hosts[0]
    with pytest.raises(Invalid, match="load 9"):
        hosts.check_limits(CONFIG, config_host, Profile.BUILD, ignore=False)
    hosts.check_limits(CONFIG, config_host, Profile.BUILD, ignore=True)


def test_check_limits_charges_only_recent_runs_and_keeps_single_run_fallback(
    host: Remote,
) -> None:
    config_host = CONFIG.hosts[0]
    reading = "0.5 4 33554432 12582912 0 0 - 0"
    refused = r"12582912 KiB.*3 \* reservation 6144 MiB.*2 recently admitted"
    for admitted in ("2 2", "2"):
        host.configure(limits=reading, admitted=admitted)
        with pytest.raises(Invalid, match=refused):
            hosts.check_limits(CONFIG, config_host, Profile.BUILD, ignore=False)
    for admitted in ("2 0", "0 0", None):
        host.configure(limits=reading, admitted=admitted)
        hosts.check_limits(CONFIG, config_host, Profile.BUILD, ignore=False)
    host.configure(limits="0.5 4 25165824 6291455 0 0 - -", admitted=None)
    with pytest.raises(
        Invalid, match="memory available 6291455 KiB < reservation 6144 MiB"
    ):
        hosts.check_limits(CONFIG, config_host, Profile.BUILD, ignore=False)


REAL_SHELL = transport.shell


def _stale_shell(
    monkeypatch: pytest.MonkeyPatch,
    limits_reply: str = "9 4 12582912 6291456 0 0 - -",
    slot_reply: str = "",
    fail_host: str | None = None,
) -> list[tuple[str, str]]:
    head, old = "a" * 40, f"tbhbdev 0 ({'b' * 40})"
    new = f"tbhbdev 0 ({head})"
    monkeypatch.setattr(version, "line", lambda: new)
    monkeypatch.setattr(version_check, "latest", Mock(return_value=head))
    events: list[tuple[str, str]] = []

    def shell(
        s: Settings,
        t: Host,
        script: str,
        stdin: transport.Stdin | None = None,
        *,
        timeout: float = 30,
    ) -> str:
        if script.endswith("--version"):
            events.append((t.name, "version"))
            return old if fail_host in {None, t.name} else new
        if "install failed" in script:
            events.append((t.name, "install"))
            if fail_host == t.name:
                msg = "network down"
                raise transport.TransportError(msg)
            return f"{'b' * 40} {head} ok 0.1s"
        if fail_host is not None:
            return REAL_SHELL(s, t, script, stdin, timeout=timeout)
        if "# tbhbdev-limits" in script:
            assert "alpha agent admitted" in script
            events.append((t.name, "limits"))
            return limits_reply
        events.append((t.name, "check-slot"))
        return slot_reply

    monkeypatch.setattr(transport, "shell", shell)
    return events


def test_stale_host_is_healed_before_limits_admitted_and_slot(
    host: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = dataclasses.replace(LINUX, name="host-1", auto_select=True)
    cfg = dataclasses.replace(CONFIG, hosts=(candidate,))
    monkeypatch.setattr(agent_settings, "load", lambda: cfg)
    cases = [
        ("host-1", "9 4 12582912 6291456 0 0 - -", ""),
        ("host-1", "0.5 4 12582912 1048576 0 0 - -", ""),
        (None, "0.5 4 12582912 6291456 0 0 - -", admission.SLOT_FULL),
    ]
    for target, limits_reply, slot_reply in cases:
        events = _stale_shell(monkeypatch, limits_reply, slot_reply)
        assert launch(host, target=target) == 1
        tail = ["check-slot"] if slot_reply else []
        assert [s for _, s in events] == ["version", "install", "limits", *tail]


def test_failed_heal_and_once_per_invocation(
    host: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h1 = dataclasses.replace(LINUX, name="host-1", auto_select=True)
    h2 = dataclasses.replace(LINUX, name="host-2", auto_select=True)
    config = dataclasses.replace(CONFIG, hosts=(h1, h2))
    monkeypatch.setattr(agent_settings, "load", lambda: config)
    monkeypatch.setattr(version_check, "latest", Mock(side_effect=AssertionError))
    hosts.check_limits(CONFIG, CONFIG.hosts[0], Profile.BUILD, ignore=False)

    events = _stale_shell(monkeypatch)
    with pytest.raises(Invalid, match="load 9 >= limit 4"):
        hosts.check_limits(config, h1, Profile.BUILD, ignore=False)
    args = argparse.Namespace(profile="build", account=None)
    assert hosts.usable_hosts(config, h1.os, args, 1e9) == []
    assert [n for n, step in events if step == "install"] == ["host-1", "host-2"]

    events = _stale_shell(monkeypatch, fail_host="host-1")
    assert launch(host, target="host-1") == 1
    err = capsys.readouterr().err
    assert "heal failed: network down" in err
    assert "available alternatives: host-2" in err
    assert launch(host, target=None) == 0
    assert metadata.load(config, "sample").host.name == "host-2"
    assert [n for n, step in events if step == "install"] == ["host-1", "host-1"]
