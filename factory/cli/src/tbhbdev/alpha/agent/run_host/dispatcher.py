"""Claim a run and record failures at the host command boundary."""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from tbhbdev import userconfig
from tbhbdev.alpha.agent import build_info, ownership, process_group, runfiles
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.agent.ownership import RunExistsError
from tbhbdev.alpha.agent.run_host import execution, intake
from tbhbdev.alpha.agent.run_host.state import RunInterruptedError


def main(
    arguments: list[str],
    prog: str,
    *,
    claimed: bool = False,
    build: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("name", nargs="?")
    parser.add_argument("request", nargs="?")
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--prompt")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--owned", action="store_true")
    parser.add_argument("--steer", action="store_true")
    args = parser.parse_args(arguments)
    if args.name is None:
        print(f"{prog}: run name is required", file=sys.stderr)
        return 1
    completed = None
    recorded = None
    previous = {s: signal.signal(s, interrupt) for s in process_group.INTERRUPTS}
    try:
        with process_group.defer_signals():
            files = intake.files_for(args.name)
            refuse_started(files)
            ownership.claim(files, claimed=claimed, owned=args.owned)
        # Anything that fails before this lock is held, including taking the
        # lock, belongs to a waiter and writes nothing: the records are the
        # run that holds the lock.
        with runfiles.locked(files.path("run.lock")):
            # Outside the try below: a refusal here, or an interrupt that
            # replaces it, must leave the earlier run's records alone.
            refuse_started(files)
            try:
                completed = _execute(args, build)
            except _REFUSALS as exc:
                if completed is None and not isinstance(exc, RunExistsError):
                    # Ignored until `recorded` is set: a signal in between
                    # would return a different status than the rc file holds.
                    with process_group.ignore_signals():
                        recorded = _recorded_rc(files)
                        if recorded is None:
                            _record_failure(files, exc)
                            recorded = _failure_rc(exc)
                raise
    except _REFUSALS as exc:
        if completed is not None:
            return completed
        print(f"{prog}: {exc}", file=sys.stderr)
        # An interrupt while the lock is released replaces the exception but
        # not the result already written to the rc file.
        return recorded if recorded is not None else _failure_rc(exc)
    else:
        return completed
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _execute(args: argparse.Namespace, build: str | None) -> int:
    # Inside the caller's try so a failure to create the temp directory
    # leaves a record like any other refusal.
    temporary = tempfile.TemporaryDirectory()
    try:
        run, text = intake.load(args, Path(temporary.name).resolve())
        if build is not None:
            run.files.write("build", build)
        run.files.write("log", build_info.line() + "\n")
        return execution.execute(run, text, preflight_only=args.preflight)
    finally:
        # The run's result takes precedence over temporary cleanup.
        with suppress(Exception):
            temporary.cleanup()


def refuse_started(files: runfiles.RunFiles) -> None:
    if files.path("rc").exists() or files.path("pgid").exists():
        message = f"run {files.name!r} has already started"
        raise RunExistsError(message)


def interrupt(_number: int, _frame: object) -> None:
    if process_group.cleaning():
        return
    for number in process_group.INTERRUPTS:
        signal.signal(number, signal.SIG_IGN)
    raise RunInterruptedError


_REFUSALS = (
    Invalid,
    userconfig.ConfigError,
    OSError,
    ValueError,
    RunInterruptedError,
)


def _failure_rc(exc: Exception) -> int:
    return 143 if isinstance(exc, RunInterruptedError) else 1


def _record_failure(files: runfiles.RunFiles, exc: Exception) -> None:
    """Write the failed run's records while the caller holds the run lock."""
    with process_group.ignore_signals():
        try:
            reason = (
                "interrupted during load"
                if isinstance(exc, RunInterruptedError)
                else str(exc)
            )
            files.write("log", f"run refused: {reason}\n")
        finally:
            files.write("rc", f"{_failure_rc(exc)}\n")


def _recorded_rc(files: runfiles.RunFiles) -> int | None:
    """The status `execute` already wrote, when a signal hit before main saw it."""
    path = files.path("rc")
    return int(path.read_text()) if path.exists() else None
