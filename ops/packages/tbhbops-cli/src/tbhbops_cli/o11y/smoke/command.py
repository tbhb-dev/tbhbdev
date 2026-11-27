"""Dispatch the observability smoke command and report its result."""

import errno
import os.path
import re
import signal
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbops.o11y import init, rustfs_probe, smoke

from tbhbops_cli.common import CliError
from tbhbops_cli.o11y import init as initialization
from tbhbops_cli.o11y import render
from tbhbops_cli.o11y import validate as validation
from tbhbops_cli.o11y.smoke import (
    alerts,
    bootstrap,
    capacity,
    diagnostics,
    exports,
    grafana,
    lifecycle,
    monitoring,
    readiness,
    setup,
    telemetry,
)
from tbhbops_cli.o11y.smoke.runtime import Smoke, environment
from tbhbops_cli.o11y.smoke.safety import (
    SmokeError,
    require,
    safe_path,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Generator
    from typing import TextIO

    from tbhbops_cli.cli import System
    from tbhbops_cli.o11y.filesystem import Filesystem


def _terminated(number: int, _frame: object) -> None:
    raise SmokeError(smoke.FAILURES["interrupted"], 128 + number)


@contextmanager
def signals() -> Generator[None]:
    previous = {}
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, _terminated)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def dispatch(args: argparse.Namespace, system: System, out: TextIO) -> int:
    with signals():
        return _dispatch(args, system, out)


def _operation_failure(exc: Exception, run: Smoke | None) -> str:
    error = type(exc).__name__
    if isinstance(exc, OSError) and exc.errno in errno.errorcode:
        error += smoke.MESSAGES["smoke_operation_errno"].format(
            name=errno.errorcode[exc.errno]
        )
    progress = smoke.MESSAGES["smoke_before_group_1"]
    if run is not None:
        progress = smoke.MESSAGES["smoke_operation_progress"].format(
            group=run.group, step=run.step
        )
    line = smoke.MESSAGES["fail_smoke_operation_unavailable"].format(
        error=error, progress=progress
    )
    # Only init._inspect's repr-quoted relative path is safe to disclose.
    if isinstance(exc, initialization.PlanIncompleteError) and re.fullmatch(
        r"""init: can't inspect (?:'(?:[^'\\\r\n]|\\.)*'|"(?:[^"\\\r\n]|\\.)*")"""
        r"; plan incomplete",
        str(exc),
    ):
        line = smoke.MESSAGES["smoke_operation_plan_incomplete"].format(
            failure=line, message=exc
        )
    return line


def _dispatch(
    args: argparse.Namespace, system: System, out: TextIO
) -> int:  # explicit exit and allocation cleanup states
    fs = system.filesystem
    run = None
    temporary = None
    status = 1
    try:
        target, artifacts, source = (
            Path(os.path.abspath(system.cwd / p))  # refuse links first
            for p in (args.out, args.artifacts, args.source)
        )  # resolve would follow links before refusing them
        for path in (target, artifacts):
            safe_path(fs, path)
        if any(
            a == b or a in b.parents or b in a.parents
            for a, b in ((target, artifacts), (target, source), (artifacts, source))
        ):
            raise SmokeError(
                smoke.FAILURES["usage_overlapping_paths"], 2
            )  # usage status is handled with the other safe failures
        temporary = fs.temporary_directory()
        safe_path(fs, temporary)
        project = "o11y-smoke-" + system.random_bytes(4).hex()
        data = Path(
            rustfs_probe.compose_data_dir(system.platform == "linux") or temporary
        )
        if system.platform != "linux":
            data = data.with_name(project.removeprefix("o11y-"))
        env = environment(system.env, data, (system.euid, system.egid))
        sentinels = smoke.sentinels(
            (
                *(system.random_bytes(s.length) for s in init.SECRETS),
                system.random_bytes(16),
                system.random_bytes(16),
            )
        )
        run = Smoke(system, target, artifacts, temporary, data, project, sentinels, env)
        run.dedicated = args.dedicated_engine
        _execute(run, source)
        status = 0
    except SmokeError as exc:
        status = exc.status
        _failure(run, out, f"FAIL {exc}")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        CliError,
        initialization.PlanIncompleteError,
    ) as exc:
        _failure(run, out, _operation_failure(exc, run))
    finally:
        _finish(run, temporary, fs, status, out)
    return status


def _execute(run: Smoke, source: Path) -> None:
    mode = "local" if run.dedicated is None else f"dedicated {run.dedicated}"
    run.emit(f"engine mode: {mode}")
    local = validation.local_engine(run.system, run.env, dedicated=run.dedicated)
    require(local is None, smoke.FAILURES["setup_local_engine_refused"])
    for port in smoke.PORTS:
        if not run.system.ports.available(port):
            raise SmokeError(smoke.FAILURES["usage_loopback_port_already_bound"], 2)
    setup.prepare(run, source)
    run.stage("group 1", setup.group1, run)
    run.stage("group 2", bootstrap.group2, run)
    run.stage("group 3", readiness.group3, run)
    run.stage("group 3a", readiness.group3a, run)
    run.stage("group 4", grafana.group4, run)
    run.stage("group 5", exports.group5, run)
    run.stage("export persistence", exports.export_persistence, run)
    run.stage("capacity", capacity.smoke, run, source)
    run.stage("group 6", exports.group6, run)
    run.stage("group 7", monitoring.group7, run, source)
    run.stage("group 8", lifecycle.group8, run, source)
    run.stage("group 9", lifecycle.group9, run)
    run.stage("group 10", lifecycle.group10, run)
    run.stage("group 11", telemetry.group11, run)
    run.stage("group 12", alerts.group12, run, source)
    run.stage("group 13", lifecycle.group13, run, source)
    run.stage("capacity cleanup", capacity.cleanup, run)
    run.finalize_transcript()
    diagnostics.scans(run)


def _finish(
    run: Smoke | None, temporary: Path | None, fs: Filesystem, status: int, out: TextIO
) -> None:
    if run is not None:
        diagnostics.finish(run, status, out)
    elif temporary is not None:
        try:
            render.remove_tree(fs, temporary)
        except BaseException:  # noqa: BLE001  # preserve an allocation or interruption failure
            out.write(smoke.MESSAGES["smoke_diagnostics_cleanup_failed"] + "\n")


def _failure(run: Smoke | None, out: TextIO, message: str) -> None:
    if run is not None:
        run.emit(message)
    else:
        out.write(message + "\n")
