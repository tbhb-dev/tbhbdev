# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Track Claude Bash commands and match them to their agents."""

import time
import uuid
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.identity import commands as identity_commands
from tbhbdev.alpha.identity.payload import Json, as_str, rec_ts
from tbhbdev.alpha.identity.sessions import session_stores
from tbhbdev.alpha.identity.storage import (
    locked,
    read_record,
    state_dir,
    user_store,
    write_json,
    write_private_json,
)
from tbhbdev.alpha.identity.values import hx, sha256_64

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Only used by the optional rewrite path, which stays off unless
# ASCM_AGENT_REWRITE=1 is set at hook time (it costs a permission prompt).
AGENT_ENV = "ASCM_AGENT_ID"

CMD_WINDOW = 2 * 3600


def rec_hashes(rec: Json) -> set[str]:
    """A stored command record's command hashes (our own store: trusted)."""
    hashes = rec.get("cmd", [])
    return set(cast("list[str]", hashes))  # pragma: no mutate -- a runtime no-op


def command_records(store: Path, root: str) -> Iterator[tuple[Path, Json]]:
    """Pending (not done) command records for a root session in one store."""
    for path in (store / "cmds" / hx(root)).glob("*.json"):
        rec = read_record(path)
        if rec and not rec.get("done"):
            yield path, rec


def record_command(
    root: str, agent: str | None, cmd: object, cwd: Path | None = None
) -> None:
    """Record a Bash command about to run, by hash, with the agent running it."""
    if not isinstance(cmd, str):
        return
    state = state_dir(cwd)
    name = f"{uuid.uuid4().hex}.json"
    rec: Json = {
        "agent": agent,
        "cmd": identity_commands.cmd_hashes(cmd),
        "ts": time.time(),
        "done": None,
    }
    if state:
        write_json(state / "cmds" / hx(root) / name, rec)
    # A copy in the user's store lets a commit in another clone find the agent,
    # and is the only record of a session whose directory is no repository.
    try:
        user_cmds = user_store() / "cmds" / hx(root)
        # The chmod below sets the mode whatever this one was, so the pragma
        # hides equivalent mutants.
        user_cmds.mkdir(mode=0o700, parents=True, exist_ok=True)  # pragma: no mutate
        user_store().chmod(0o700)
        if state:
            references = user_store() / "cmd-stores" / hx(root)
            references.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private_json(
                references / f"{hx(sha256_64(str(state)))}.json", {"store": str(state)}
            )
        write_private_json(user_cmds / name, rec)
    except OSError:
        return


def finish_command(
    root: str,
    cmd: object,
    agent: str | None,
    cwd: Path | None = None,
    *,
    user_only: bool = False,
) -> None:
    """Mark the newest pending record for (agent, command) done.

    A later identical command is then unambiguous. ``user_only`` leaves the
    directory's repository alone, as in another owner's repository.
    """
    if not isinstance(cmd, str):
        return
    if user_only and not user_store().exists():
        return
    state = None if user_only else state_dir(cwd)
    hashes, done = set(identity_commands.cmd_hashes(cmd)), time.time()
    stores = [(user_store(), write_private_json)]
    if state:
        stores.insert(0, (state, write_json))
    for store, write in stores:
        try:
            with locked(store):
                candidates = [
                    (path, rec)
                    for path, rec in command_records(store, root)
                    if rec.get("agent") == agent and hashes & rec_hashes(rec)
                ]
                best = max(candidates, key=lambda item: rec_ts(item[1]), default=None)
                if best:
                    best[1]["done"] = done
                    write(*best)
        except OSError:
            continue


class AttributionError(Exception):
    """A plausible child committer could not be identified safely."""


def recent_commands(
    root: str, cwd: Path | None, now: float, *, project: Path | None = None
) -> list[Json]:
    """Pending commands for this root that overlap the attribution window."""
    return [
        rec
        for store in [*session_stores(root, cwd, project=project), user_store()]
        for _, rec in command_records(store, root)
        if 0 <= now - rec_ts(rec) < CMD_WINDOW
    ]


def match_command_agent(  # noqa: PLR0913 -- lookup inputs and commit policy
    root: str,
    command_lines: list[str],
    cwd: Path | None = None,
    now: float | None = None,
    *,
    strict: bool = False,
    project: Path | None = None,
) -> tuple[str | None, bool]:
    """Match ancestry to a recorded command: (agent hash, ambiguous).

    None means the main agent (or no match). Strict commit-time matching refuses
    an unresolved identity while a child command overlaps the time window.
    """
    records = recent_commands(
        root, cwd, time.time() if now is None else now, project=project
    )
    hashes = {
        h
        for line in command_lines
        for cand in identity_commands.command_candidates(line)
        for h in identity_commands.cmd_hashes(cand)
    }
    agents = {as_str(rec.get("agent")) for rec in records if hashes & rec_hashes(rec)}
    if len(agents) == 1:
        return agents.pop(), False
    if strict and any(as_str(rec.get("agent")) for rec in records):
        message = (
            "Attribution couldn't be resolved for a commit made while a subagent "
            "was active. Retry the commit from the main session after the "
            "subagent's command finishes."
        )
        raise AttributionError(message)
    return None, bool(agents)  # exactly one agent returned above
