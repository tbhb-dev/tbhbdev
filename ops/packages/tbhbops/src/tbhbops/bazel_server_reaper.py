"""Retire orphaned agent Bazel servers; standalone for system Python on lab hosts."""

import argparse
import json
import os
import pwd
import re
import signal
import sys
import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import cast

ORPHAN_SECONDS = 300
MAX_OBSERVATION_GAP = 90


@dataclass(frozen=True)
class Process:
    pid: int
    started: str
    argv: tuple[str, ...]
    uid: int

    @property
    def run(self) -> str | None:
        # argv[0] keeps the full Bazel title; Linux comm truncates it to 15 bytes.
        match = re.fullmatch(
            r"bazel\(agent-([A-Za-z0-9][A-Za-z0-9._-]*)\)", self.argv[0]
        )
        return match[1] if match else None


def live_runs(processes: list[Process], uid: int) -> set[str]:
    return {
        run
        for process in processes
        if process.uid == uid
        for command, run in pairwise(process.argv)
        if command.split("/")[-1] == "run-host"
    }


class ProcessTable:
    def __init__(self, uid: int, root: Path = Path("/proc")) -> None:
        self.uid = uid
        self.root = root

    def read(self, pid: int) -> Process:
        directory = self.root / str(pid)
        started = (directory / "stat").read_text().rsplit(")", 1)[1].split()[19]
        argv = tuple(
            (directory / "cmdline")
            .read_bytes()
            .decode(errors="surrogateescape")
            .rstrip("\0")
            .split("\0")
        )
        return Process(pid, started, argv, directory.stat().st_uid)

    def snapshot(self) -> list[Process]:
        processes = []
        for directory in self.root.iterdir():
            if not directory.name.isdecimal():
                continue
            try:
                processes.append(self.read(int(directory.name)))
            except FileNotFoundError, ProcessLookupError:
                # Exited during enumeration. Unreadable processes instead fail closed.
                continue
        return processes

    def term(self, process: Process) -> bool:
        if sys.platform == "linux":
            try:
                descriptor = os.pidfd_open(process.pid)
                try:
                    # Pin the PID, then recheck both identity and the run-host table.
                    if self.read(process.pid) != process or process.uid != self.uid:
                        return False
                    if process.run in live_runs(self.snapshot(), self.uid):
                        return False
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                    return True
                finally:
                    os.close(descriptor)
            except FileNotFoundError, ProcessLookupError:
                return False
        msg = "Bazel server reaping requires Linux pidfds"
        raise RuntimeError(msg)


def reap(
    table: ProcessTable,
    previous: dict[str, list[int]],
    now: int,
    boot: str,
    *,
    dry_run: bool = True,
) -> dict[str, list[int]]:
    processes = table.snapshot()
    active = live_runs(processes, table.uid)
    remaining = {}
    action = "DRYRUN" if dry_run else "TERM"
    for process in processes:
        run = process.run
        if process.uid != table.uid or run is None or run in active:
            continue
        identity = f"{boot}:{process.pid}:{process.started}:{run}"
        first, last = previous.get(identity, [now, now])
        # A missed minute or a clock rollback breaks consecutive observations.
        if not 0 <= now - last <= MAX_OBSERVATION_GAP:
            first = now
        age = now - first
        if age >= ORPHAN_SECONDS and (dry_run or table.term(process)):
            print(f"bazel-reaper: {action} pid={process.pid} run={run} age={age}")
        else:
            # A failed final recheck can mean a run-host returned: restart grace.
            remaining[identity] = [now if age >= ORPHAN_SECONDS else first, now]
    return remaining


def main(
    state: Path = Path("/var/lib/bazel-server-reaper/state.json"),
    argv: list[str] | None = None,
) -> None:
    if sys.platform == "linux":
        parser = argparse.ArgumentParser()
        parser.add_argument("--live", action="store_true")
        parser.add_argument("--user", default="agent")
        args = parser.parse_args(argv)
        uid = pwd.getpwnam(args.user).pw_uid
        previous = (
            cast(
                "dict[str, list[int]]",  # pragma: no mutate # Type hint only.
                json.loads(state.read_text()),
            )
            if state.exists()
            else {}
        )
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        now = int(time.clock_gettime(time.CLOCK_BOOTTIME))
        remaining = reap(ProcessTable(uid), previous, now, boot, dry_run=not args.live)
        # The staging suffix's case does not affect atomic replacement.
        pending = state.with_suffix(".new")  # pragma: no mutate
        pending.write_text(json.dumps(remaining))
        pending.replace(state)
        return
    msg = "Bazel server reaping requires Linux procfs"
    raise RuntimeError(msg)


if __name__ == "__main__":
    main()
