"""Store and retrieve private local run records."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.driver.decisions.metadata import (
    check_name,
    parse_record,
    record_fields,
)
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def directory() -> Path:
    return runfiles.state_home(Path.home()) / files.DIRECTORY


def metadata_path(name: str) -> Path:
    check_name(name)
    return directory() / f"{name}.json"


def save(run: Run) -> None:
    path = metadata_path(run.name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    record = record_fields(run)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(record, output)
        output.write("\n")


def load(settings: Settings, name: str, *, archived: Path | None = None) -> Run:
    path = metadata_path(name) if archived is None else archived
    if archived is None:
        path.parent.chmod(0o700)
    try:
        return parse_record(settings, name, path.read_text())
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        message = f"{path}: invalid run metadata"
        raise Invalid(message) from exc


@contextmanager
def launch_lock() -> Generator[None]:
    """Hold the coordinator-side lock that orders duplicate checks and saves."""
    folder = directory()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    descriptor = os.open(
        folder / "launch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def archive_metadata(path: Path, stamp: str) -> None:
    trash = directory() / "trash" / stamp
    trash.mkdir(mode=0o700, parents=True)
    path.rename(trash / path.name)


def not_owner(run: Run) -> NoReturn:
    archive_metadata(metadata_path(run.name), datetime.now(UTC).isoformat())
    message = f"{run.name}: not owner"
    raise Invalid(message)
