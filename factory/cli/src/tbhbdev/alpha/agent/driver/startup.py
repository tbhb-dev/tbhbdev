"""Observe a remote launch and unwind unconfirmed startup."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import shims, transport
from tbhbdev.alpha.agent.decisions import remote
from tbhbdev.alpha.agent.driver.cleanup import clean
from tbhbdev.alpha.agent.driver.decisions import lifecycle as rules
from tbhbdev.alpha.agent.driver.decisions.output import launched, sandbox
from tbhbdev.alpha.agent.driver.lifecycle import close_session, kill
from tbhbdev.alpha.agent.driver.metadata import not_owner

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def recover_transfer(settings: Settings, run: Run) -> None:
    try:
        transport.shell(
            settings, run.host, remote.owned(run.name), transport.Stdin(run.owner)
        )
    except transport.CollisionError:
        not_owner(run)
    except transport.TransportError:
        # An unreachable host may have started the run. Keep its owner claim
        # so later verbs can reach it once the connection recovers.
        pass


def poll_launch(settings: Settings, run: Run, deadline: float) -> int:
    poll_deadline = deadline
    while True:
        reading = transport.read(
            settings,
            run.host,
            (run.name, run.owner),
            timeout=poll_deadline - time.monotonic(),
        )
        poll_deadline = rules.launch_deadline(
            reading.phase, deadline, shims.SHIM_SECONDS
        )
        action = rules.launch(
            reading.rc,
            launched=reading.launched,
            expired=time.monotonic() >= poll_deadline,
        )
        if action == "finished":
            # The host may still be closing its pane after committing the rc.
            close_session(settings, run)
            print(
                f"{run.name}: rc={reading.rc}"
                f"{sandbox(run.request.authorized_by)}"
                f"\n{reading.text}",
                end="",
            )
            if reading.rc and not reading.launched:
                # A refusal before the harness starts must release both claims
                # so the name can be used again. Keep the refusal log in trash.
                clean(settings, run, force=True)
            return reading.rc or 0
        if action == "launched":
            print(
                launched(
                    run.name,
                    run.host.name,
                    run.host.account,
                    authorized_by=run.request.authorized_by,
                )
            )
            return 0
        if action == "deadline":
            return unconfirmed(settings, run, reading.phase)
        time.sleep(0.5)


def unconfirmed(settings: Settings, run: Run, phase: str) -> int:
    print(
        f"{run.name}: launch unconfirmed host={run.host.name} phase={phase}"
        + sandbox(run.request.authorized_by)
    )
    try:
        kill(settings, run)
    except transport.CollisionError:
        not_owner(run)
    return 1
