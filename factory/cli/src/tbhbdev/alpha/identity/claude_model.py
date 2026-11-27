# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read Claude transcripts to resolve models and active subagents."""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.claude_commands import CMD_WINDOW, AttributionError
from tbhbdev.alpha.identity.payload import (
    Json,
    as_dict,
    as_str,
    first_str,
    parse_line,
    read_tail,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

SESSION_RE = re.compile(r"[0-9A-Za-z_-]+")

CLAUDE_TAIL = 256 * 1024


def real_model(value: object) -> str | None:
    """A model name; None when it is empty or a ``<...>`` one, like ``<synthetic>``."""
    model = as_str(value)
    return model if model and not model.startswith("<") else None


def entry_time(value: object) -> float:
    """A transcript ``timestamp`` in seconds; 0 when it is missing or unreadable."""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def transcript_entry(path: object) -> tuple[str, float] | None:
    """Claude: the last real assistant model in a transcript tail, and its time."""
    lines = read_tail(Path(path), CLAUDE_TAIL) if isinstance(path, str) else None
    for line in reversed(lines or []):
        entry = parse_line(line) or {}
        message = as_dict(entry.get("message"))
        model = real_model(message.get("model")) if message else None
        if model:
            return model, entry_time(entry.get("timestamp"))
    return None


def transcript_model(path: object) -> str | None:
    """Claude: the last real assistant ``message.model`` in a transcript tail."""
    found = transcript_entry(path)
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
    switched, when = as_str(rec.get("switch_model")), rec.get("switch_at")
    if switched and isinstance(when, float) and not (found and found[1] >= when):
        return switched
    if found:
        return found[0]
    return first_str(rec, "model", "payload_model")


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
    if not recent:
        return False
    lines = read_tail(path, CLAUDE_TAIL)
    if lines is None:
        return True
    for line in reversed(lines):
        entry = parse_line(line) or {}
        if entry.get("type") in {"assistant", "user"}:
            message = as_dict(entry.get("message")) or {}
            return (
                entry["type"] != "assistant" or message.get("stop_reason") != "end_turn"
            )
    return False


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
