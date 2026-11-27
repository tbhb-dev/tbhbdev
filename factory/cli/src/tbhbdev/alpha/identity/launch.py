# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Claim launch metadata and record the start of an agent session."""

import time
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.sessions import load_record
from tbhbdev.alpha.identity.storage import locked, read_record, state_dir, write_json
from tbhbdev.alpha.identity.trailers import start_ok, utc
from tbhbdev.alpha.identity.values import hash_ok

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.identity.payload import Json

LAUNCH_FILE = "launch.json"


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
        rec = read_record(path)
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
