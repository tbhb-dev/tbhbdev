"""Archive remote run files and local records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import remote_cleanup
from tbhbdev.alpha.agent.driver.decisions.lifecycle import clean as check_clean
from tbhbdev.alpha.agent.driver.metadata import (
    archive_metadata,
    directory,
    load,
    metadata_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def clean(
    settings: Settings, run: Run, *, force: bool, archived: Path | None = None
) -> int:
    path = metadata_path(run.name) if archived is None else archived
    # An archived record may only recover a missing claim. The host still
    # refuses an existing owner or a live run.
    ownerless_only = True
    if archived is None:
        try:
            reading = transport.read(settings, run.host, (run.name, run.owner))
        except transport.CollisionError:
            pass
        else:
            check_clean(reading.rc, session=reading.session, force=force)
            ownerless_only = False
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    transport.shell(
        settings,
        run.host,
        remote_cleanup.clean(
            run.request.repo,
            run.name,
            stamp,
            settings.sockets_dir,
            force=force,
            ownerless_only=ownerless_only,
        ),
        transport.Stdin(run.owner),
    )
    archive_metadata(path, stamp)
    return 0


def clean_archived(settings: Settings, name: str, *, force: bool) -> int:
    archived = max(
        (directory() / "trash").glob(f"*/{name}.json"),
        key=lambda path: path.parent.stat().st_mtime_ns,
        default=None,
    )
    # Completed cleanups use a compact UTC stamp ending in Z. Ownership
    # failures use an ISO stamp ending in +00:00 and may leave host files.
    if archived is None or archived.parent.name.endswith("Z"):
        return 0
    run = load(settings, name, archived=archived)
    return clean(settings, run, force=force, archived=archived)


def keep(settings: Settings, run: Run) -> int:
    transport.shell(
        settings, run.host, remote_cleanup.keep(run.name), transport.Stdin(run.owner)
    )
    return 0
