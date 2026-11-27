"""Find live Bazel servers from the host process table and Bazel metadata."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess  # nosec B404
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tbhbdev.alpha.agent import host_tools, process_group, runfiles
from tbhbdev.alpha.agent.model import Invalid

MEMORY_DISK = Path("/dev") / "shm"
_OUTPUT_BASE = re.compile(r" --output_base=(/.*?) --workspace_directory=")


@dataclass(frozen=True)
class Process:
    parent: int
    group: int
    command: str


@dataclass(frozen=True)
class Server:
    pid: int
    workspace: Path
    belongs: bool
    started: str


def listing(uid: int) -> str:
    """Read numeric process IDs and argv on macOS and Linux."""
    try:
        return subprocess.check_output(  # nosec B603
            [
                host_tools.resolve("ps"),
                "-ww",
                "-u",
                str(uid),
                "-o",
                "uid=,pid=,ppid=,pgid=,args=",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            timeout=5,
        )
    except Invalid, OSError, subprocess.SubprocessError:
        return ""


def _processes(uid: int) -> dict[int, Process]:
    processes: dict[int, Process] = {}
    for line in listing(uid).splitlines():
        try:
            owner, pid, parent, group, command = line.split(maxsplit=4)
            if int(owner) == uid:
                processes[int(pid)] = Process(int(parent), int(group), command)
        except ValueError:
            continue
    return processes


def _run_pids(processes: dict[int, Process], group: int | None) -> set[int]:
    children: defaultdict[int, list[int]] = defaultdict(list)
    pending = [os.getpid()]
    for pid, process in processes.items():
        children[process.parent].append(pid)
        if process.group == group:
            pending.append(pid)
    owned = set(pending)
    while pending:
        for pid in children[pending.pop()]:
            if pid not in owned:
                owned.add(pid)
                pending.append(pid)
    return owned


def pid_text(base: Path) -> str:
    return runfiles.read_owned(base / "server/server.pid.txt", 64, "Bazel PID").strip()


def matches_pid(base: Path, pid: int) -> bool:
    try:
        status = base.lstat()
        return (
            stat.S_ISDIR(status.st_mode)
            and status.st_uid == os.getuid()
            and pid_text(base) == str(pid)
        )
    except OSError, ValueError:
        return False


def _server_workspace(base: Path) -> Path | None:
    try:
        # Bazel writes NUL-separated argv here, preserving spaces in paths.
        args = runfiles.read_owned(base / "server/cmdline", 65536, "Bazel argv").split(
            "\0"
        )
    except OSError, ValueError:
        return None
    for arg in args:
        if arg.startswith("--workspace_directory=/"):
            workspace = Path(arg.removeprefix("--workspace_directory="))
            return workspace if ".." not in workspace.parts else None
    return None


def find(worktree: Path, group: int | None) -> dict[Path, Server]:
    processes = _processes(os.getuid())
    owned = _run_pids(processes, group)
    servers: dict[Path, Server] = {}
    for pid, process in processes.items():
        if not process.command.startswith("bazel("):
            continue
        match = _OUTPUT_BASE.search(process.command)
        if match is None:
            continue
        base = Path(match[1])
        if ".." in base.parts:
            continue
        if not matches_pid(base, pid):
            continue
        workspace = _server_workspace(base)
        started = process_group.leader_start(pid)
        if workspace is not None and started is not None:
            belongs = pid in owned or workspace.is_relative_to(worktree)
            servers[base.resolve()] = Server(pid, workspace, belongs, started)
    return servers


def stop(base: Path, server: Server) -> str:
    """Stop the identified process even when the server cannot handle RPCs."""
    if not matches_pid(base, server.pid):
        return "server changed"
    started = process_group.leader_start(server.pid)
    if started != server.started:
        return "rc=0" if started is None else "server changed"
    try:
        _kill(server)
    except ProcessLookupError:
        return "rc=0"
    except PermissionError:
        return "permission denied"
    for _ in range(10):
        if process_group.leader_start(server.pid) != server.started:
            return "rc=0"
        time.sleep(0.02)
    return "server alive"


def _kill(server: Server) -> None:
    if sys.platform != "linux":
        os.kill(server.pid, signal.SIGKILL)
        return
    fd = os.pidfd_open(server.pid)
    try:
        if process_group.leader_start(server.pid) == server.started:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)
