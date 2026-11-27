"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from pathlib import Path
lazy from collections.abc import Callable

import pytest
from agent_host_example import (
    constant,
)
from run_host_fixtures import (
    HOST_FIXTURES,
    bazel_base,
    bazel_run,
    fake_bazel,
    make_base,
)

from tbhbdev.alpha.agent import (
    bazel_servers,
)
from tbhbdev.alpha.agent.run_host import bazel_shutdown as host_bazel_shutdown
from tbhbdev.alpha.agent.run_host import completion as host_completion
from tbhbdev.alpha.agent.run_host import environment as host_environment

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


def test_shutdown_bazel_pins_the_recorded_output_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    calls: list[list[str]] = []
    fake_bazel(monkeypatch, calls=calls)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    assert calls == [["/bin/bazelisk", f"--output_base={base}", "shutdown"]]
    status = base.lstat()
    assert stopped == {base: ("rc=0", (status.st_dev, status.st_ino))}


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
    descendant, grouped, foreign = (
        tmp_path / name for name in ("custom base", "grouped", "foreign")
    )
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
    fake_bazel(monkeypatch)
    calls: list[tuple[str, str]] = []

    def shutdown(
        command: list[str], *, cwd: str, **_options: object
    ) -> subprocess.CompletedProcess[bytes]:
        executable, output_base, verb = command
        assert (executable, verb) == ("/bin/bazelisk", "shutdown")
        path = output_base.removeprefix("--output_base=")
        calls.append((path, cwd))
        if Path(path) == base:
            raise subprocess.TimeoutExpired(command, 60)
        (Path(path) / "server/server.pid.txt").unlink()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", shutdown)
    run.bazel_shutdown = host_bazel_shutdown.shutdown_bazel(run, prepared, group=group)
    assert host_completion.finish(run, prepared, None, 7) == 7
    assert calls == [
        (str(recorded), str(worktree)),
        (str(base), str(nested)),
        (str(descendant.resolve()), str(worktree)),
        (str(grouped.resolve()), str(sibling)),
    ]
    log = run.files.path("log").read_text().splitlines()
    assert [line for line in log if line.startswith("bazel shutdown:")] == [
        f"bazel shutdown: {path!r} ({'timed out' if Path(path) == base else 'rc=0'})"
        for path, _ in calls
    ]
    assert not recorded.exists()
    assert all(path.is_dir() for path in (base, descendant, grouped, other, foreign))


@pytest.mark.parametrize("available", [False, True])
def test_shutdown_bazel_uses_recorded_bases_without_listed_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_bazel_listing: Callable[[int], str],
    available: bool,
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    calls: list[list[str]] = []
    fake_bazel(monkeypatch, calls=calls)

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

    monkeypatch.setattr(bazel_servers, "listing", empty_bazel_listing)
    monkeypatch.setattr(subprocess, "check_output", listing)
    assert list(host_bazel_shutdown.shutdown_bazel(run, prepared, group=7)) == [base]
    assert calls == [["/bin/bazelisk", f"--output_base={base}", "shutdown"]]


@pytest.mark.parametrize("problem", ["tool missing", "pid changed"])
def test_shutdown_bazel_logs_unavailable_discovered_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    run, prepared = bazel_run(tmp_path)
    workspace = Path(prepared.paths.worktree).resolve()
    base = tmp_path.resolve() / "custom"
    row = server_row(base, workspace, 17, 1, 1)
    monkeypatch.setattr(bazel_servers, "listing", constant(row))
    if problem == "tool missing":
        monkeypatch.setattr(host_bazel_shutdown, "bazel_tool", constant(None))
        status = "tool unavailable"
    else:
        original = bazel_servers.matches_pid
        matches = iter([True, False])

        def changed(path: Path, pid: int) -> bool:
            return original(path, pid) and next(matches)

        monkeypatch.setattr(bazel_servers, "matches_pid", changed)
        monkeypatch.setattr(
            host_bazel_shutdown, "bazel_tool", constant("/bin/bazelisk")
        )
        status = "server changed"
    assert host_bazel_shutdown.shutdown_bazel(run, prepared, group=7) == {
        base: (status, None)
    }
    expected = f"bazel shutdown: {str(base)!r} ({status})\n"
    assert run.files.path("log").read_text() == expected


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
    assert bazel_servers.find(workspace, 17) == {}
    for args in ("bazel(workspace)", "--workspace_directory=/workspace/../other"):
        cmdline.write_text(args)
        assert bazel_servers.find(workspace, 17) == {}

    link = tmp_path / "link"
    link.symlink_to(base)
    assert not bazel_servers.matches_pid(link, 17)
    monkeypatch.setattr(os, "getuid", constant(uid + 1))
    assert not bazel_servers.matches_pid(base, 17)


@pytest.mark.parametrize(
    ("available", "prepared"),
    [(False, True), (True, False)],
)
def test_shutdown_bazel_skips_without_a_tool_or_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: bool, prepared: bool
) -> None:
    run, value = bazel_run(tmp_path)
    make_base(value)
    fake_bazel(monkeypatch)
    if not available:

        def missing(_name: str, *, path: str) -> None:
            assert path

        monkeypatch.setattr(shutil, "which", missing)
    assert host_bazel_shutdown.shutdown_bazel(run, value if prepared else None) == {}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (subprocess.TimeoutExpired("bazelisk", 60), "timed out"),
        (OSError(), "failed to run"),
    ],
)
def test_shutdown_bazel_reports_a_failed_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    run, prepared = bazel_run(tmp_path)
    make_base(prepared)
    fake_bazel(monkeypatch)

    def fail(*_args: object, **_options: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    assert [status for status, _ in stopped.values()] == [expected]


def test_shutdown_bazel_searches_the_configured_tool_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    searched: list[str | None] = []
    fake_bazel(monkeypatch)

    def which(name: str, path: str | None = None) -> str:
        searched.append(path)
        return f"/bin/{name}"

    monkeypatch.setattr(shutil, "which", which)
    host_bazel_shutdown.shutdown_bazel(run, prepared)
    tools = host_environment.tool_path(run.settings, run.host, str(run.home))
    assert searched[0] is not None
    assert searched[0].endswith(":" + tools)
