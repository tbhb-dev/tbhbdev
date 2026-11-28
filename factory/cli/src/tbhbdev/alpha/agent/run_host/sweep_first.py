"""Sweep this machine once before a run is admitted; never block the launch."""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import process_group

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import Run

SWEEP_LIMIT_SECONDS = 60


def command() -> tuple[str, ...]:
    """Run this build's sweep; the host's PATH and bin folder decide nothing."""
    return (sys.executable, "-I", "-m", "tbhbdev", "alpha", "agent", "sweep")


def run_sweep(run: Run) -> None:
    """Run the sweep within the limit and log one line if it did not succeed.

    The time the sweep takes moves the run's deadline, as shim builds do.
    """
    started = time.monotonic()
    try:
        failure = _sweep(run.settings.kill_grace_seconds)
    finally:
        run.deadline += time.monotonic() - started
    if failure is not None:
        with suppress(OSError):
            run.files.write("log", f"sweep: {failure}; the launch goes on\n")


def _sweep(grace: float) -> str | None:
    devnull = subprocess.DEVNULL
    try:
        child = process_group.launch(
            command(), env=dict(os.environ), cwd="/", streams=(devnull,) * 3
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not start ({type(exc).__name__})"
    try:
        rc = child.wait(timeout=SWEEP_LIMIT_SECONDS)
    except subprocess.TimeoutExpired:
        return f"stopped after {SWEEP_LIMIT_SECONDS}s"
    finally:
        if child.poll() is None:
            with process_group.ignore_signals(), suppress(OSError):
                process_group.stop_started(child.pid, grace, child.poll)
    return None if rc == 0 else f"exited {rc}"
