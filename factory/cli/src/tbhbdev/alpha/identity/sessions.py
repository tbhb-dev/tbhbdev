# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Persist harness sessions and claim launch records."""

from __future__ import annotations

import time
from pathlib import Path

from tbhbdev.alpha.identity import storage
from tbhbdev.alpha.identity.records import Json, as_str, hash_ok, hx, merge_records
from tbhbdev.alpha.identity.storage import (
    locked,
    state_dir,
    stores,
    user_store,
    write_json,
    write_private_json,
)
from tbhbdev.alpha.identity.trailers import start_ok, utc

LAUNCH_FILE = "launch.json"


def write_record(
    sid_hash: str, updates: Json, cwd: Path | None = None, once: Json | None = None
) -> None:
    """Merge the non-empty ``updates`` into an agent's session record.

    The record goes to the store of the worktree containing ``cwd``, if any,
    and to the user's store. The non-empty ``once`` fields are set only where
    the record lacks them, so a resumed session keeps its first values.
    """
    known = {
        k: v for k, v in updates.items() if v
    }  # never overwrite known with missing
    first = {k: v for k, v in (once or {}).items() if v}
    state = state_dir(cwd)
    if state:
        path = state / "sessions" / f"{hx(sid_hash)}.json"
        with locked(state):
            rec = storage.read_record(path)
            rec.update(known)
            write_json(path, {**first, **rec})
    write_user_record(sid_hash, known, first)


def claim_launch(root: str, cwd: Path | None = None) -> Json:
    """Claim the worktree's launch file for a root session; its launch fields.

    The launcher writes ``launch`` and ``started_at`` there before the harness
    starts. The first root session to run a hook writes its hash in as
    ``session``. A file another session claimed, or one with invalid fields,
    gives an empty dict.
    """
    state = state_dir(cwd)
    if not state:
        return {}
    path = state / LAUNCH_FILE
    with locked(state):
        rec = storage.read_record(path)
        fields: Json = {
            "launch": hash_ok(rec.get("launch")),
            "started_at": start_ok(rec.get("started_at")),
        }
        if not all(fields.values()) or rec.get("session", root) != root:
            return {}
        if "session" not in rec:
            write_json(path, {**rec, "session": root})
    return fields


def start_fields(agent: str, root: str, cwd: Path | None = None) -> Json:
    """The set-once launch fields an agent's hook event gives its record.

    A root session takes them from the launch file it claims, or the time of
    this event when it has none to claim. A subagent takes the time of this
    event and its root's ``launch``. An agent whose record has a
    ``started_at`` already gets an empty dict, or only the ``launch`` it lacks.
    """
    rec = load_record(agent, cwd, root=root)
    if start_ok(rec.get("started_at")):
        # A root that began elsewhere still claims its launch file for ``launch``.
        claiming = agent == root and not rec.get("launch")
        launch = claim_launch(root, cwd) if claiming else None
        return {"launch": launch["launch"]} if launch else {}
    now = utc(time.time())
    if agent != root:
        return {"started_at": now, "launch": load_record(root, cwd).get("launch")}
    return claim_launch(root, cwd) or {"started_at": now}


def write_user_record(sid_hash: str, updates: Json, once: Json | None = None) -> None:
    """Merge ``updates`` into the user's store, a 0700 directory of 0600 files.

    A store the hook can't write, such as one outside a sandbox, is skipped,
    since the repository's store already holds the record. ``once`` fields are
    set only where the record lacks them.
    """
    user = user_store()
    path = user / f"{hx(sid_hash)}.json"
    try:
        # The chmod below sets the mode whatever this one was, so the pragma
        # hides equivalent mutants. It's needed for a directory that exists.
        user.mkdir(mode=0o700, parents=True, exist_ok=True)  # pragma: no mutate
        user.chmod(0o700)
        with locked(user):
            rec = storage.read_record(path)
            rec.update(updates)
            write_private_json(
                path, {**{k: v for k, v in (once or {}).items() if v}, **rec}
            )
    except OSError:
        return


def session_stores(
    root: str, cwd: Path | None = None, *, project: Path | None = None
) -> list[Path]:
    """The root project's stores and registered stores, with this clone last."""
    local = stores(cwd)
    registered = (
        [store for store in stores(project) if store not in local] if project else []
    )
    for path in (user_store() / "cmd-stores" / hx(root)).glob("*.json"):
        store = as_str(storage.read_record(path).get("store"))
        if store and Path(store) not in local and Path(store) not in registered:
            registered.append(Path(store))
    return [*registered, *local]


def load_record(
    hashed: str,
    cwd: Path | None = None,
    *,
    root: str | None = None,
    project: Path | None = None,
) -> Json:
    """An agent's session record, merged across worktrees; an empty dict if none.

    The record in the worktree containing ``cwd`` wins, field by field, and
    the user's store fills in the rest, for a session that commits in another
    clone. Registered command stores are also searched; ``root`` names the
    owning session when loading a child. A ``/model`` switch is the exception:
    its model and time come as a pair from whichever store has the latest one,
    because the session may have switched again while its hooks ran in another
    worktree.
    """
    records = [storage.read_record(user_store() / f"{hx(hashed)}.json")]
    records += [
        storage.read_record(store / "sessions" / f"{hx(hashed)}.json")
        for store in session_stores(root or hashed, cwd, project=project)
    ]
    return merge_records(records)
