"""Wait for, check, and stop remote runs."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import process_group, runfiles, transport
from tbhbdev.alpha.agent.decisions import remote_run, run_state
from tbhbdev.alpha.agent.driver.decisions import lifecycle as rules
from tbhbdev.alpha.agent.driver.decisions import output
from tbhbdev.alpha.agent.driver.decisions.lifecycle import answer_state, state
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def status_local(files: runfiles.RunFiles) -> int:
    phase = None
    rc = "unknown"
    with suppress(FileNotFoundError):
        rc = str(int(runfiles.read_owned(files.path("rc"), 64, "return code")))
    with suppress(FileNotFoundError):
        phase = runfiles.read_owned(files.path("phase"), 64, "run phase").strip()
    if files.path("keep").is_file():
        phase = "kept"
    elif phase is None and rc != "unknown":
        phase = "ended"
    elif phase is None and files.path("request.json").is_file():
        phase = "launching"
    elif phase is None and any(
        (files.root / "trash").glob(f"????????T????????????Z-{files.name}")
    ):
        phase = "cleaned"
    elif phase is None:
        message = f"{files.name}: unknown run; local record absent"
        raise Invalid(message)
    print(f"{files.name}: phase={phase} rc={rc} (local record absent)")
    return 0


def _setup_record(files: runfiles.RunFiles) -> bool:
    """Whether the group on record is the setup worker's, so not yet the harness's."""
    phase = None
    with suppress(FileNotFoundError):
        phase = runfiles.read_owned(files.path("phase"), 64, "run phase").strip()
    return run_state.setup_record(phase, launched=files.path("launched").exists())


def kill_local(files: runfiles.RunFiles, wait_seconds: int) -> int:
    deadline = time.monotonic() + wait_seconds
    while True:
        with runfiles.try_lock(files.path("run.lock")) as unlocked:
            if files.path("rc").is_file():
                print("already finished")
                return 0
            if not _setup_record(files):
                with suppress(FileNotFoundError):
                    recorded = process_group.read_record(files)
                    return process_group.kill_recorded(files, recorded)
            if unlocked:
                message = (
                    f"{files.name}: no process group recorded and run lock is not held"
                )
                raise Invalid(message)
        if time.monotonic() >= deadline:
            message = (
                f"{files.name}: timed out waiting for the run to record a process group"
            )
            raise Invalid(message)
        time.sleep(0.1)


def wait(settings: Settings, run: Run) -> int:
    unreachable = 240
    failed_since = None
    while True:
        attempt = time.monotonic()
        timeout = (
            unreachable
            if failed_since is None
            else unreachable - (attempt - failed_since)
        )
        try:
            reading = transport.read(
                settings,
                run.host,
                (run.name, run.owner),
                answer=True,
                timeout=min(30, timeout),
            )
        except transport.TransportError:
            now = time.monotonic()
            if failed_since is None:
                failed_since = attempt
            result = rules.wait(
                None,
                session=True,
                failed_seconds=now - failed_since,
                unreachable=unreachable,
            )
            if result is not None:
                print(
                    f"{run.name}: rc=unreachable"
                    + output.sandbox(run.request.authorized_by)
                )
                print(output.INSPECTION_WARNING)
                return result
        else:
            failed_since = None
            result = rules.wait(
                reading.rc,
                session=reading.session,
                failed_seconds=0,
                unreachable=unreachable,
            )
            if result is not None:
                rc = answer_state(reading.rc)
                print(
                    f"{run.name}: rc={rc}"
                    f"{output.sandbox(run.request.authorized_by)}\n{reading.text}",
                    end="",
                )
                print("\n" + output.INSPECTION_WARNING)
                print(output.worktree_kept(run.name, run.host.name, run.request.repo))
                return result
        time.sleep(30)


def status(settings: Settings, run: Run) -> int:
    try:
        reading = transport.read(
            settings,
            run.host,
            (run.name, run.owner),
            reason=True,
        )
    except transport.TransportError:
        # The host can't answer, so the run may still be live. Print the
        # local record and say the state is unknown instead of failing bare.
        print(
            output.status(
                run.name,
                phase="unknown",
                rc="unknown",
                target=(
                    run.host.name,
                    run.host.account,
                    run.request.authorized_by,
                    run.launched_by[0],
                ),
                started=run.started,
            )
            + " host unreachable, run state unknown"
        )
        return 1
    rc = state(reading.rc, session=reading.session)
    if reading.rc and reading.text:
        rc += f" ({reading.text.rstrip()})"
    print(
        output.status(
            run.name,
            phase=reading.phase,
            rc=rc,
            target=(
                run.host.name,
                run.host.account,
                run.request.authorized_by,
                run.launched_by[0],
            ),
            started=run.started,
        )
    )
    return 0


def close_session(settings: Settings, run: Run) -> None:
    transport.shell(
        settings,
        run.host,
        remote_run.session(run.name, "kill-session", settings.sockets_dir),
        transport.Stdin(run.owner),
    )


def kill(settings: Settings, run: Run) -> int:
    close_session(settings, run)
    deadline = time.monotonic() + settings.kill_grace_seconds + 2
    while True:
        remaining = deadline - time.monotonic()
        reading = transport.read(
            settings,
            run.host,
            (run.name, run.owner),
            timeout=min(30, max(1, remaining)),
        )
        action = rules.kill(
            reading.rc, session=reading.session, expired=time.monotonic() >= deadline
        )
        if action == "finished":
            return 0
        if action == "write":
            transport.shell(
                settings,
                run.host,
                remote_run.fallback(run.name, settings.sockets_dir),
                transport.Stdin(run.owner),
            )
            return 0
        if action == "refuse":
            message = f"{run.name}: session still exists; return code left to run-host"
            raise Invalid(message)
        time.sleep(0.5)
