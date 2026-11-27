# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read model and turn information from Claude transcript content."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePath

from tbhbdev.alpha.identity.records import Json, as_dict, as_str, first_str, parse_line

SESSION_RE = re.compile(r"[0-9A-Za-z_-]+")


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


def latest_model_entry(lines: list[str] | None) -> tuple[str, float] | None:
    """The final model entry and timestamp in the supplied transcript lines."""
    for line in reversed(lines or []):
        entry = parse_line(line) or {}
        message = as_dict(entry.get("message"))
        model = real_model(message.get("model")) if message else None
        if model:
            return model, entry_time(entry.get("timestamp"))
    return None


def choose_main_model(rec: Json, found: tuple[str, float] | None) -> str | None:
    """Choose between a model switch, transcript entry and stored model."""
    switched, when = as_str(rec.get("switch_model")), rec.get("switch_at")
    if switched and isinstance(when, float) and not (found and found[1] >= when):
        return switched
    if found:
        return found[0]
    return first_str(rec, "model", "payload_model")


def child_transcript(data: Json) -> str | None:
    """A Claude subagent's transcript path.

    The path derives from the main transcript:
    ``<main transcript sans .jsonl>/subagents/agent-<agent_id>.jsonl``.
    """
    transcript, agent_id = as_str(data.get("transcript_path")), data.get("agent_id")
    if transcript and agent_id:
        return str(
            PurePath(transcript).with_suffix("")
            / "subagents"
            / f"agent-{agent_id}.jsonl"
        )
    return None


def unfinished_transcript(lines: list[str] | None) -> bool:
    """Whether the transcript lacks a final assistant reply that ended its turn."""
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
