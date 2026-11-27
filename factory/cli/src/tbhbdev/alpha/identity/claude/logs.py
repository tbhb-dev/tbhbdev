# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Locate and read Claude session transcripts and metadata."""

from __future__ import annotations

import json
import time
from pathlib import Path
lazy from collections.abc import Mapping

from tbhbdev.alpha.identity.claude.matching import CMD_WINDOW
from tbhbdev.alpha.identity.claude.transcript import (
    SESSION_RE,
    choose_main_model,
    latest_model_entry,
    unfinished_transcript,
)
from tbhbdev.alpha.identity.records import AttributionError, Json
from tbhbdev.alpha.identity.storage import read_tail

CLAUDE_TAIL = 256 * 1024


def transcript_entry(path: object, size: int | None = None) -> tuple[str, float] | None:
    """Claude: the last real assistant model in a transcript tail, and its time."""
    lines = (
        read_tail(Path(path), size or CLAUDE_TAIL) if isinstance(path, str) else None
    )
    return latest_model_entry(lines)


def transcript_model(path: object, size: int | None = None) -> str | None:
    """Claude: the last real assistant ``message.model`` in a transcript tail."""
    found = transcript_entry(path, size)
    return found[0] if found else None


def main_model(rec: Json) -> str | None:
    """The Claude main agent's model now, from its session record.

    In order: the newer of the last ``/model`` switch and the transcript's last
    real assistant entry (the transcript wins a tie, and an entry without a
    readable time counts as older); else that entry; else the model a hook last
    read from the transcript, for a transcript gone since; else a model a hook
    payload carried; else None. The transcript is read now, because a commit's
    first turn has no entry until after its hooks ran, and a switch has none
    until the next reply.
    """
    found = transcript_entry(rec.get("transcript"))
    return choose_main_model(rec, found)


def claude_config(env: Mapping[str, str]) -> Path:
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path(env.get("HOME", "")) / ".claude")


def session_model(raw: str, env: Mapping[str, str]) -> str | None:
    """Claude: the model in the session's own transcript under its config dir.

    A run that disables hooks writes no session record, so the record has no
    transcript path. Claude names each transcript after its session ID.
    """
    if not SESSION_RE.fullmatch(raw):
        return None
    for transcript in sorted(claude_config(env).glob(f"projects/*/{raw}.jsonl")):
        if model := transcript_model(str(transcript)):
            return model
    return None


def unfinished_turn(path: Path, now: float) -> bool:
    """Claude: whether a recent transcript doesn't end on a finished assistant turn."""
    try:
        recent = now - path.stat().st_mtime < CMD_WINDOW
    except OSError:
        return False
    return recent and unfinished_transcript(read_tail(path, CLAUDE_TAIL))


def stopped_by_user(path: Path) -> bool:
    """Claude: whether the subagent's metadata records that the user stopped it."""
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text())
    except OSError, ValueError:
        return False
    return isinstance(meta, dict) and meta.get("stoppedByUser") is True


def subagent_running(raw: str, env: Mapping[str, str], now: float) -> bool:
    """Claude: whether a subagent of the session may be running right now.

    A subagent's transcript sits under the session's directory. It is
    unfinished until its last turn is an assistant reply that ended the turn.
    A transcript that can't be read counts as unfinished. A subagent whose
    metadata says the user stopped it isn't running, whatever its transcript ends on.
    """
    if not SESSION_RE.fullmatch(raw):
        return False
    pattern = f"projects/*/{raw}/subagents/agent-*.jsonl"
    return any(
        unfinished_turn(path, now) and not stopped_by_user(path)
        for path in claude_config(env).glob(pattern)
    )


def hookless_model(raw: str, env: Mapping[str, str], *, sub: str | None) -> str | None:
    """Claude: the main model from the transcript when no hook record names one.

    A subagent's commit can't be told from the main agent's then: both carry
    the session's ID. Refuse rather than stamp the main model on its work.
    """
    if not sub and subagent_running(raw, env, time.time()):
        message = (
            "A subagent of this session is running and no hook record says "
            "which agent is committing. Commit from the main session after "
            "the subagent finishes."
        )
        raise AttributionError(message)
    return session_model(raw, env)
