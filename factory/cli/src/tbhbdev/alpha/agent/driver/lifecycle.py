"""Wait for, check, and stop remote runs."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import remote
from tbhbdev.alpha.agent.driver.decisions import lifecycle as rules
from tbhbdev.alpha.agent.driver.decisions import output
from tbhbdev.alpha.agent.driver.decisions.lifecycle import (
    answer_state,
    state,
    wait_kind,
)
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def wait(settings: Settings, run: Run) -> int:
    wait_kind(run.request.kind)
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
        remote.session(run.name, "kill-session", settings.sockets_dir),
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
                remote.fallback(run.name, settings.sockets_dir),
                transport.Stdin(run.owner),
            )
            return 0
        if action == "refuse":
            message = f"{run.name}: session still exists; return code left to run-host"
            raise Invalid(message)
        time.sleep(0.5)
