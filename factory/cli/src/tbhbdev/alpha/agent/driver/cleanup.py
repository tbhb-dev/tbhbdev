"""Archive remote run files and local records."""

from __future__ import annotations

import json
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import process_group, runfiles, transport
from tbhbdev.alpha.agent.decisions import remote_cleanup, repo, request, runfiles_names
from tbhbdev.alpha.agent.driver.decisions.lifecycle import clean as check_clean
from tbhbdev.alpha.agent.driver.metadata import (
    archive_metadata,
    directory,
    load,
    metadata_path,
)
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def clean(
    settings: Settings, run: Run, *, force: bool, archived: Path | None = None
) -> str:
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
    moved = transport.shell(
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
    if archived is None or not archived.parent.name.endswith("Z"):
        archive_metadata(path, stamp)
        moved += f"moved {path.name}\n"
    return moved


def clean_archived(settings: Settings, name: str, *, force: bool) -> int:
    archived = max(
        (directory() / "trash").glob(f"*/{name}.json"),
        key=lambda path: path.parent.stat().st_mtime_ns,
        default=None,
    )
    if archived is None:
        return clean_local(runfiles.RunFiles(directory(), name), force=force)
    run = load(settings, name, archived=archived)
    print(clean(settings, run, force=force, archived=archived), end="")
    return 0


def clean_local(files: runfiles.RunFiles, *, force: bool) -> int:
    with runfiles.try_lock(files.path("run.lock")) as unlocked:
        running = not unlocked
        with suppress(FileNotFoundError):
            running |= process_group.alive(process_group.read_record(files).pgid)
        names = (files.root / f"{files.name}{s}" for s in runfiles_names.SUFFIXES)
        paths = [path for path in names if path.exists(follow_symlinks=False)]
        if not paths:
            return 0
        rc = 0 if files.path("rc").is_file() else None
        check_clean(rc, session=running, force=force)
        trash = files.root / "trash"
        _plain(files.root.parent, files.root, trash)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = trash / f"{stamp}-{files.name}"
        tree = _local_worktree(files)
        target.mkdir(mode=0o700, parents=True)
        if tree is not None:
            shutil.move(tree, target / "worktree")
            print("moved worktree")
        for path in paths:
            path.rename(target / path.name)
            print(f"moved {path.name}")
    return 0


def _plain(*paths: Path) -> None:
    for path in paths:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            message = f"clean refused: {path} is not a plain directory"
            raise Invalid(message)


def _local_worktree(files: runfiles.RunFiles) -> Path | None:
    """The run's worktree on this host, or None when there is none to move.

    The request names the clone, so find it before the run files move away.
    """
    try:
        text = runfiles.read_owned(files.path("request.json"), 65536, "request")
        data = json.loads(text)
        clone = Path(repo.clone_path(Path.home(), request.parse(data).repo))
    except FileNotFoundError, ValueError, Invalid:
        return None
    tree = clone / ".worktrees" / f"agent-{files.name}"
    _plain(clone, clone / ".worktrees", tree)
    return tree if tree.is_dir() else None


def keep(settings: Settings, run: Run) -> int:
    transport.shell(
        settings, run.host, remote_cleanup.keep(run.name), transport.Stdin(run.owner)
    )
    return 0
