"""Sweep this machine before a run is admitted."""

from __future__ import annotations

import os
import time
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import sweep
from tbhbdev.alpha.agent.run_host.state import RunInterruptedError

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import Run


def run_sweep(run: Run) -> None:
    """Sweep in process, logging failures and extending the run's deadline."""
    started = time.monotonic()
    try:
        with (
            Path(os.devnull).open("w") as sink,
            redirect_stdout(sink),
            redirect_stderr(sink),
        ):
            rc = sweep.main([], "tbhbdev alpha agent sweep")
    except Exception as exc:
        if isinstance(exc, RunInterruptedError):
            raise
        failure = f"failed ({type(exc).__name__})"
    else:
        if rc == 0:
            return
        failure = f"exited {rc}"
    finally:
        run.deadline += time.monotonic() - started
    with suppress(OSError):
        run.files.write("log", f"sweep: {failure}; the launch goes on\n")
