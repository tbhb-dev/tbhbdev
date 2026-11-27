"""Read and export the committed changes and output of a run."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import remote_inspection, remote_run
from tbhbdev.alpha.agent.model import Invalid, Profile
from tbhbdev.alpha.terminal import terminal_text

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def export_patch(settings: Settings, run: Run) -> int:
    if run.request.profile is not Profile.BUILD:
        message = "diff --patch requires a finished build run"
        raise Invalid(message)
    script = remote_inspection.export_patch(run.request.repo, run.name)
    output = transport.call_bytes(
        settings, run.host, ("/bin/sh", "-c", script), transport.Stdin(run.owner)
    )
    sys.stdout.buffer.write(output)
    return 0


def inspect(settings: Settings, run: Run, *, limit: int | None) -> int:
    if (
        run.request.unsandboxed
        and limit is None
        and run.request.profile is Profile.REVIEW
    ):
        reading = transport.read(
            settings, run.host, (run.name, run.owner), answer=True, timeout=30
        )
        print(terminal_text(reading.text), end="")
        return 0
    script = (
        remote_run.run_log(run.name)
        if run.request.unsandboxed and limit is not None
        else remote_inspection.inspect(run.request.repo, run.name, limit=limit)
    )
    print(
        terminal_text(
            transport.shell(
                settings,
                run.host,
                script,
                transport.Stdin(run.owner),
            )
        ),
        end="",
    )
    return 0
