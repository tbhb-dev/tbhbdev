import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import override

import pytest

from tbhbops import bazel_server_reaper as reaper

SERVER = reaper.Process(42, "100", ("bazel(agent-e1)", "-jar", "server.jar"), 1000)
HOST = reaper.Process(43, "101", ("tbhbdev", "alpha", "run-host", "e1"), 1000)
IDENTITY = "boot:42:100:e1"


@pytest.fixture(autouse=True)
def signal_guard(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    registered: set[int] = set()

    def guarded(target: int, *_args: object) -> int:
        if target not in registered:
            msg = f"Unregistered signal target: {target}"
            raise AssertionError(msg)
        return 99

    # One shared monkeypatch undoes in reverse order, so tests can patch over it.
    monkeypatch.setattr(os, "kill", guarded)
    monkeypatch.setattr(os, "pidfd_open", guarded, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", guarded, raising=False)
    return registered


@pytest.fixture(autouse=True)
def linux_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(time, "CLOCK_BOOTTIME", 7, raising=False)


class FakeTable(reaper.ProcessTable):
    def __init__(self, processes: list[reaper.Process]) -> None:
        super().__init__(1000)
        self.processes = processes
        self.signalled: list[reaper.Process] = []

    @override
    def snapshot(self) -> list[reaper.Process]:
        return self.processes

    @override
    def term(self, process: reaper.Process) -> bool:
        self.signalled.append(process)
        return True


@pytest.mark.parametrize(
    ("name", "run"),
    [
        ("bazel(agent-e1)", "e1"),
        ("bazel(agent-long.run_1-2)", "long.run_1-2"),
        ("bazel(typescript)", None),
        ("bazel(agent-e1)extra", None),
        ("bazel(agent-e1 other)", None),
    ],
)
def test_names(name: str, run: str | None) -> None:
    assert reaper.Process(1, "1", (name,), 1000).run == run


@pytest.mark.parametrize("dry_run", [False, True])
def test_five_consecutive_minutes(
    capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    table = FakeTable([SERVER])
    state: dict[str, list[int]] = {}
    for now in range(1000, 1300, 60):
        state = reaper.reap(table, state, now, "boot", dry_run=dry_run)
        assert state == {IDENTITY: [1000, now]}
        assert table.signalled == []
    assert reaper.reap(table, state, 1300, "boot", dry_run=dry_run) == {}
    assert table.signalled == ([] if dry_run else [SERVER])
    action = "DRYRUN" if dry_run else "TERM"
    assert capsys.readouterr().out == f"bazel-reaper: {action} pid=42 run=e1 age=300\n"


@pytest.mark.parametrize("elapsed", [-1, 0, 60, 90, 91, 300])
def test_observation_gaps(elapsed: int) -> None:
    table = FakeTable([SERVER])
    first = 1100 if 0 <= elapsed <= 90 else 1240 + elapsed
    assert reaper.reap(table, {IDENTITY: [1100, 1240]}, 1240 + elapsed, "boot") == {
        IDENTITY: [first, 1240 + elapsed]
    }
    assert table.signalled == []


def test_live_run_resets_age_and_prunes_gone_servers() -> None:
    table = FakeTable(
        [SERVER, HOST, reaper.Process(44, "102", ("bazel(typescript)",), 1000)]
    )
    assert reaper.reap(table, {IDENTITY: [1000, 1240]}, 1300, "boot") == {}
    assert table.signalled == []
    table.processes = [SERVER]
    assert reaper.reap(table, {}, 1360, "boot") == {IDENTITY: [1360, 1360]}


def test_exact_run_host_arguments() -> None:
    processes = [
        HOST,
        reaper.Process(44, "1", ("/opt/run-host", "e2", "--flag"), 1000),
        reaper.Process(45, "1", ("run-host-extra", "e3"), 1000),
        reaper.Process(46, "1", ("run-host",), 1000),
    ]
    assert reaper.live_runs(processes, 1000) == {"e1", "e2"}


@pytest.mark.parametrize(
    "identity", ["old:42:100:e1", "boot:42:99:e1", "boot:41:100:e1", "boot:42:100:e2"]
)
def test_boot_pid_start_and_run_identity(identity: str) -> None:
    assert reaper.reap(FakeTable([SERVER]), {identity: [1000, 1240]}, 1300, "boot") == {
        IDENTITY: [1300, 1300]
    }


def test_proc_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "42"
    directory.mkdir()
    (directory / "stat").write_text(
        "42 (bazel(agent-e1)) " + " ".join(["0"] * 19 + ["100", "200"])
    )
    (directory / "cmdline").write_bytes(b"bazel(agent-e1)\0-jar\0server.jar\0")
    (tmp_path / "not-a-pid").mkdir()
    (tmp_path / "43").mkdir()

    def directories(path: Path) -> list[Path]:
        assert path == tmp_path
        return [tmp_path / "43", tmp_path / "not-a-pid", directory]

    monkeypatch.setattr(Path, "iterdir", directories)
    table = reaper.ProcessTable(1000, tmp_path)
    assert table.snapshot() == [replace(SERVER, uid=directory.stat().st_uid)]
    (directory / "cmdline").write_bytes(b"cmdX\0")
    assert table.read(42).argv == ("cmdX",)
    (directory / "cmdline").write_bytes(b"")
    assert table.read(42).run is None
    (directory / "cmdline").write_bytes(b"bad\xff\0")
    assert table.read(42).argv == ("bad" + chr(0xDCFF),)


@pytest.mark.parametrize("error", [PermissionError, ProcessLookupError])
def test_unreadable_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    (tmp_path / "42").mkdir()
    table = reaper.ProcessTable(1000, tmp_path)

    def read(_pid: int) -> reaper.Process:
        raise error

    monkeypatch.setattr(table, "read", read)
    if error is PermissionError:
        with pytest.raises(PermissionError):
            table.snapshot()
    else:
        assert table.snapshot() == []


SEND_ERRORS = {"gone-signal": ProcessLookupError, "denied": PermissionError}
READ_RESULTS = {
    "reused": replace(SERVER, started="other"),
    "owner": replace(SERVER, uid=2000),
}
EXPECTED_EVENTS: dict[str, list[tuple[str, int]]] = {
    "gone-open": [],
    "term": [("term", 99), ("close", 99)],
    "foreign-host": [("term", 99), ("close", 99)],
}


@pytest.mark.parametrize(
    "case",
    [
        "term",
        "reused",
        "owner",
        "wrong-user",
        "live",
        "foreign-host",
        "gone-open",
        "gone-read",
        "gone-signal",
        "denied",
    ],
)
def test_pidfd_revalidation(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    table = reaper.ProcessTable(2000 if case == "wrong-user" else 1000)
    events: list[tuple[str, int]] = []

    def open_pid(pid: int) -> int:
        assert pid == 42
        if case == "gone-open":
            raise ProcessLookupError
        return 99

    def read(pid: int) -> reaper.Process:
        assert pid == 42
        if case == "gone-read":
            raise FileNotFoundError
        return READ_RESULTS.get(case, SERVER)

    def send(descriptor: int, number: int) -> None:
        assert (descriptor, number) == (99, signal.SIGTERM)
        if case in SEND_ERRORS:
            raise SEND_ERRORS[case]
        events.append(("term", descriptor))

    monkeypatch.setattr(reaper.os, "pidfd_open", open_pid, raising=False)

    def close(fd: int) -> None:
        events.append(("close", fd))

    monkeypatch.setattr(reaper.os, "close", close)
    monkeypatch.setattr(reaper.signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(table, "read", read)
    live = [HOST] if case == "live" else [replace(HOST, uid=2000)]
    monkeypatch.setattr(table, "snapshot", lambda: live)
    if case == "denied":
        with pytest.raises(PermissionError):
            table.term(SERVER)
    else:
        assert table.term(SERVER) is (case in {"term", "foreign-host"})
    assert events == EXPECTED_EVENTS.get(case, [("close", 99)])


def test_failed_term_restarts_grace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    table = FakeTable([SERVER])

    def term(_process: reaper.Process) -> bool:
        return False

    monkeypatch.setattr(table, "term", term)
    state = {IDENTITY: [1000, 1240]}
    assert reaper.reap(table, state, 1300, "boot", dry_run=False) == {
        IDENTITY: [1300, 1300]
    }
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("dry_run", [False, True])
def test_state_persists_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    state = tmp_path / "state.json"
    table = FakeTable([SERVER])

    def clock(clock_id: int) -> float:
        if sys.platform == "linux":
            assert clock_id == time.CLOCK_BOOTTIME
        return 1000.9

    def read(path: Path) -> str:
        if path == state:
            return state.read_bytes().decode()
        assert str(path) == "/proc/sys/kernel/random/boot_id"
        return "boot\n"

    def process_table(uid: int) -> FakeTable:
        assert uid == 1000
        return table

    def user(name: str) -> SimpleNamespace:
        assert name == ("builder" if dry_run else "agent")
        return SimpleNamespace(pw_uid=1000)

    monkeypatch.setattr(reaper.pwd, "getpwnam", user)
    monkeypatch.setattr(reaper, "ProcessTable", process_table)
    monkeypatch.setattr(reaper.time, "clock_gettime", clock)
    monkeypatch.setattr(Path, "read_text", read)
    argv = ["--user", "builder"] if dry_run else ["--live"]
    monkeypatch.setattr(sys, "argv", ["bazel-reaper", *argv])
    reaper.main(state)
    assert json.loads(state.read_bytes()) == {IDENTITY: [1000, 1000]}
    # An argv passed in must win over sys.argv.
    monkeypatch.setattr(sys, "argv", ["bazel-reaper", "--user", "other"])
    reaper.main(state, argv)
    assert json.loads(state.read_bytes()) == {IDENTITY: [1000, 1000]}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state.json"]
    state.write_text(json.dumps({IDENTITY: [700, 940]}))
    reaper.main(state, argv)
    assert json.loads(state.read_bytes()) == {}
    action = "DRYRUN" if dry_run else "TERM"
    assert capsys.readouterr().out == f"bazel-reaper: {action} pid=42 run=e1 age=300\n"
    assert table.signalled == ([] if dry_run else [SERVER])


def test_requires_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reaper.sys, "platform", "win32")
    with pytest.raises(
        RuntimeError, match=r"^Bazel server reaping requires Linux procfs$"
    ):
        reaper.main(tmp_path / "state.json")
    with pytest.raises(
        RuntimeError, match=r"^Bazel server reaping requires Linux pidfds$"
    ):
        reaper.ProcessTable(1000).term(SERVER)


@pytest.mark.parametrize("dry_run", [False, True])
def test_owner_filter(dry_run: bool) -> None:
    table = FakeTable([replace(SERVER, uid=2000)])
    assert (
        reaper.reap(table, {IDENTITY: [1000, 1240]}, 1300, "boot", dry_run=dry_run)
        == {}
    )
    assert table.signalled == []
    table.processes = [SERVER, replace(HOST, uid=2000)]
    reaper.reap(table, {IDENTITY: [1000, 1240]}, 1300, "boot", dry_run=dry_run)
    assert table.signalled == ([] if dry_run else [SERVER])


def test_reap_defaults_to_a_dry_run() -> None:
    table = FakeTable([SERVER])
    assert reaper.reap(table, {IDENTITY: [1000, 1240]}, 1300, "boot") == {}
    assert table.signalled == []


def test_live_run_of_the_table_owner_protects_its_servers() -> None:
    table = FakeTable([SERVER, HOST])
    state = {IDENTITY: [1000, 1240]}
    assert reaper.reap(table, state, 1300, "boot", dry_run=False) == {}
    assert table.signalled == []


def test_skipped_processes_do_not_end_the_scan() -> None:
    other_owner = replace(SERVER, pid=50, uid=2000)
    not_a_server = reaper.Process(51, "1", ("bazel(typescript)",), 1000)
    table = FakeTable([other_owner, not_a_server, SERVER])
    reaper.reap(table, {IDENTITY: [1000, 1240]}, 1300, "boot", dry_run=False)
    assert table.signalled == [SERVER]
