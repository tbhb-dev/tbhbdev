"""Stop Bazel servers that belong to a finished run."""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agent_host_example import constant, make_run
from run_host_fixtures import HOST_FIXTURES, bazel_base, bazel_run, make_base

from tbhbdev.alpha.agent import bazel_servers, process_group
from tbhbdev.alpha.agent.run_host import bazel_shutdown as host_bazel_shutdown
from tbhbdev.alpha.agent.run_host import completion as host_completion

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


def server_row(base: Path, workspace: Path, pid: int, parent: int, group: int) -> str:
    directory = base / "server"
    directory.mkdir(parents=True)
    (directory / "server.pid.txt").write_text(f"{pid}\n")
    (directory / "cmdline").write_text(
        f"bazel(workspace)\0--output_base={base}\0--workspace_directory={workspace}"
    )
    return (
        f"{os.getuid()} {pid} {parent} {group} bazel(workspace) "
        f"--output_base={base} --workspace_directory={workspace} --flag=value"
    )


def test_shutdown_bazel_discovers_and_stops_only_the_runs_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    worktree = Path(prepared.paths.worktree).resolve()
    nested = worktree / ".worktrees" / "nested --checkout"
    nested.mkdir(parents=True)
    sibling = worktree.with_name("worktree-other")
    sibling.mkdir()
    recorded = prepared.bases[0]
    other = bazel_base(run, sibling)
    prepared = dataclasses.replace(prepared, bases=(*prepared.bases, other))
    base = bazel_base(run, nested)
    memory = tmp_path / "memory"
    monkeypatch.setattr(bazel_servers, "MEMORY_DISK", memory)
    descendant = memory / "nested" / "custom base"
    grouped, foreign = tmp_path / "grouped", tmp_path / "foreign"
    uid, supervisor = os.getuid(), os.getpid()
    group = supervisor + 100
    rows = [
        "malformed row",
        f"{uid} {group} {supervisor} {group} harness",
        f"{uid} {group + 1} {group} {group + 1} shell",
        server_row(recorded, worktree, group + 2, 1, 1),
        server_row(base, nested, group + 3, 1, 1),
        server_row(descendant, sibling / "removed", group + 4, group + 1, 1),
        server_row(grouped, sibling, group + 5, 1, group),
        server_row(other, sibling, group + 6, 1, 1),
        server_row(foreign, nested, group + 7, group, group).replace(
            f"{uid} ", f"{uid + 1} ", 1
        ),
    ]
    monkeypatch.setattr(bazel_servers, "listing", constant("\n".join(rows)))
    monkeypatch.setattr(bazel_servers, "sys", SimpleNamespace(platform="darwin"))
    killed: list[int] = []

    def leader_start(pid: int) -> str | None:
        return None if pid in killed else "start"

    monkeypatch.setattr(process_group, "leader_start", leader_start)

    def kill(pid: int, number: int) -> None:
        if number == signal.SIGKILL:
            killed.append(pid)
        else:
            raise ProcessLookupError

    monkeypatch.setattr(os, "kill", kill)
    snapshot = bazel_servers.find(worktree, group)
    snapshot.pop(base)
    run.bazel_shutdown = host_bazel_shutdown.shutdown_bazel(
        run, prepared, servers=snapshot
    )
    assert host_completion.finish(run, prepared, None, 7) == 7
    assert sorted(killed) == list(range(group + 2, group + 6))
    assert all(not path.exists() for path in (recorded, base, descendant))
    assert all(path.is_dir() for path in (grouped, other, foreign))


@pytest.mark.parametrize("available", [False, True])
def test_shutdown_bazel_uses_recorded_bases_without_listed_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)

    def listing(command: list[str], **options: object) -> str:
        assert Path(command[0]).name == "ps"
        assert command[1:] == [
            "-ww",
            "-u",
            str(os.getuid()),
            "-o",
            "uid=,pid=,ppid=,pgid=,args=",
        ]
        assert options["timeout"] == 5
        if available:
            return ""
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "check_output", listing)
    assert list(host_bazel_shutdown.shutdown_bazel(run, prepared)) == [base]


def test_timed_out_run_kills_bazel_and_removes_its_output_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = prepared.bases[0]
    workspace = Path(prepared.paths.worktree)
    client = Mock(side_effect=subprocess.TimeoutExpired("bazel", 60))
    monkeypatch.setattr(subprocess, "run", client)
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"]
    ) as process:
        try:
            row = server_row(base, workspace, process.pid, os.getpid(), os.getpgrp())
            monkeypatch.setattr(bazel_servers, "listing", constant(row))
            assert host_completion.finish(run, prepared, None, 124) == 124
            assert process.wait(timeout=1) == -signal.SIGKILL
            assert not base.exists()
            client.assert_not_called()
        finally:
            if process.poll() is None:
                process.kill()


@pytest.mark.parametrize(
    ("state", "result"),
    [
        ("pidfile changed", "server changed"),
        ("pid reused", "server changed"),
        ("exited", "rc=0"),
        ("disappeared", "rc=0"),
        ("denied", "permission denied"),
        ("running", "server alive"),
        ("reused while opening", "rc=0"),
    ],
)
def test_stop_bazel_validates_the_process_and_bounds_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, result: str
) -> None:
    base = tmp_path / "base"
    server = bazel_servers.Server(17, belongs=True, started="start")
    server_row(base, tmp_path, 18 if state == "pidfile changed" else 17, 1, 1)
    starts = {
        "pid reused": ["new"],
        "exited": [None],
        "reused while opening": ["start", "new", "new"],
    }
    start = Mock(return_value="start", side_effect=starts.get(state))
    monkeypatch.setattr(process_group, "leader_start", start)
    monkeypatch.setattr(bazel_servers, "sys", SimpleNamespace(platform="linux"))
    errors = {"disappeared": ProcessLookupError, "denied": PermissionError}
    opened = Mock(return_value=42, side_effect=errors.get(state))
    sent = Mock()
    closed = Mock()
    pauses = Mock()
    monkeypatch.setattr(signal, "pidfd_send_signal", sent, raising=False)
    monkeypatch.setattr(
        bazel_servers,
        "os",
        SimpleNamespace(
            getuid=os.getuid,
            pidfd_open=opened,
            close=closed,
        ),
    )
    monkeypatch.setattr(time, "sleep", pauses)
    assert bazel_servers.stop(base, server) == result
    assert sent.call_count == int(state == "running")
    assert closed.call_count == int(state in {"running", "reused while opening"})
    assert pauses.call_count == (10 if state == "running" else 0)


def test_discovery_refuses_unconfirmed_server_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "candidate"
    workspace = tmp_path / "workspace"
    uid = os.getuid()
    row = server_row(base, workspace, 17, 1, 17)
    cmdline = base / "server/cmdline"
    rows = [
        f"{uid} 16 1 16 bazel(workspace)",
        row.replace(f"{uid} 17", f"{uid} 18", 1),
        row.replace(f"{uid} 17", f"{uid} 19", 1).replace(
            str(base), str(base / "../candidate"), 1
        ),
        row.replace(f"{uid} 17", f"{uid} 20", 1).replace(str(base), str(tmp_path), 1),
    ]
    cmdline.unlink()
    monkeypatch.setattr(bazel_servers, "listing", constant("\n".join([*rows, row])))
    monkeypatch.setattr(process_group, "leader_start", constant("start"))
    assert bazel_servers.find(workspace) == {}
    for args in ("bazel(workspace)", f"--workspace_directory={workspace}/../other"):
        cmdline.write_text(args)
        assert bazel_servers.find(workspace) == {}

    cmdline.write_text(f"--workspace_directory={tmp_path / 'other'}")
    assert bazel_servers.find(workspace) == {
        base.resolve(): bazel_servers.Server(17, belongs=False, started="start")
    }
    cmdline.write_text(f"--workspace_directory={workspace}")
    assert bazel_servers.find(workspace) == {
        base.resolve(): bazel_servers.Server(17, belongs=True, started="start")
    }
    monkeypatch.setattr(process_group, "leader_start", constant(None))
    assert bazel_servers.find(workspace) == {}

    link = tmp_path / "link"
    link.symlink_to(base)
    assert not bazel_servers.matches_pid(link, 17)
    monkeypatch.setattr(os, "getuid", constant(uid + 1))
    assert not bazel_servers.matches_pid(base, 17)


def test_shutdown_bazel_skips_without_a_workspace(tmp_path: Path) -> None:
    assert host_bazel_shutdown.shutdown_bazel(make_run(tmp_path), None) == {}
