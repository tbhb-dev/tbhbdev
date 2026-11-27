# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read process ancestry for commit and shell command attribution."""

import os
import subprocess  # nosec B404
from pathlib import Path

from tbhbdev.alpha.process import process_row

ANCESTRY_LIMIT = 16


def run_ps(pid: str) -> tuple[str, str] | None:
    """(parent PID, command line) of a process from ``ps``, or None.

    None when the process is gone, and also when ps can't run at all: a sandbox
    can deny starting it, not just its process lookup.
    """
    try:
        result = subprocess.run(  # nosec B603 B607
            ["ps", "-ww", "-o", "ppid=,command=", "-p", pid],
            capture_output=True,
            text=True,
            check=False,
        )
    except PermissionError:
        return None
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    ppid, _, cmd = line.partition(" ")
    return ppid.strip(), cmd.strip()


def ps_row(pid: str) -> tuple[str, str] | None:
    """(parent PID, command line) of a process, or None if it is gone.

    Read from /proc or libproc first, which a Codex sandbox allows; ps is the
    fallback for a platform or a denial that leaves those empty.
    """
    return process_row(pid) or run_ps(pid)


def ancestry(pid: int | None = None, limit: int = ANCESTRY_LIMIT) -> list[str]:
    """Command lines from this process up to and including the claude process."""
    out: list[str] = []
    current = str(pid or os.getpid())
    for _ in range(limit):
        row = ps_row(current)
        if row is None:
            break
        ppid, cmd = row
        out.append(cmd)
        if Path(cmd.partition(" ")[0]).name == "claude" or ppid in {"", "0", "1"}:
            break
        current = ppid
    return out
