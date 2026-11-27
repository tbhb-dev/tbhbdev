# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read and write private files in the user and worktree stores."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
lazy from collections.abc import Generator

from tbhbdev.alpha import repo as git_repo
lazy from tbhbdev.alpha.identity.records import Json

STORE = "tbhb-alpha"


@dataclass(frozen=True)
class Repo:
    """A work tree, its own store, and the store all its worktrees share."""

    top: Path
    state: Path
    shared: Path


def stores(cwd: Path | None = None) -> list[Path]:
    """Every worktree's store, the one containing ``cwd`` last.

    Empty outside a repository. The main worktree's git dir is the common dir,
    and each linked worktree's is under ``<common>/worktrees/``.
    """
    code, out = git_repo.git(
        "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", cwd=cwd
    )
    if code != 0:
        return []
    own, common = (Path(line) for line in out.split("\n"))
    dirs = [common, *sorted((common / "worktrees").glob("*"))]
    return [d / STORE for d in dirs if d != own] + [own / STORE]


def state_dir(cwd: Path | None = None) -> Path | None:
    """The store of the worktree containing ``cwd``, or None outside a repository."""
    found = stores(cwd)
    return found[-1] if found else None


def repo_info(cwd: Path | None = None) -> Repo | None:
    """The work tree containing ``cwd`` and its stores, or None."""
    code, out = git_repo.git(
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
        cwd=cwd,
    )
    if code != 0:
        return None
    top, own, common = out.split("\n")
    return Repo(Path(top).resolve(), Path(own) / STORE, Path(common) / STORE)


def read_json(path: Path, default: Json | None = None) -> Json | None:
    """Parse a JSON file; return ``default`` when it is missing or invalid."""
    try:
        loaded = json.loads(path.read_text())
    except OSError, ValueError:
        return default
    return loaded if isinstance(loaded, dict) else default


def read_record(path: Path) -> Json:
    """Parse a JSON object file; an empty dict when missing or invalid."""
    return read_json(path) or {}


def write_json(path: Path, obj: Json) -> None:
    """Write JSON atomically, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


@contextmanager
def locked(store: Path, name: str = "lock") -> Generator[None]:
    """Hold one of a store's exclusive locks for one read-modify-write.

    Never nested on the same lock: a second ``flock`` from the same process
    would deadlock. ``lock`` and ``marks.lock`` are separate files, so a scan
    may hold the shared store's ``marks.lock`` while it writes receipts.
    """
    store.mkdir(parents=True, exist_ok=True)
    with (store / name).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def read_tail(path: Path, size: int) -> list[str] | None:
    """The lines in the last ``size`` bytes of a file, or None if unreadable."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            return handle.read().decode(errors="replace").splitlines()
    except OSError:
        return None


def user_store() -> Path:
    """The user's own store, which every clone's commits can read.

    A session's hooks write to the store of its project's repository, but it
    may commit in another clone. This store keeps its record for those.
    """
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "tbhbdev" / STORE


def write_private_json(path: Path, obj: Json) -> None:
    """Write JSON atomically as a 0600 file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    # The fchmod below sets the mode whatever this one was, so the pragma hides
    # equivalent mutants. This mode only keeps the empty file private sooner.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # pragma: no mutate
    os.fchmod(fd, 0o600)  # O_CREAT's mode is ignored for a leftover temp file
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(obj))
    tmp.replace(path)
