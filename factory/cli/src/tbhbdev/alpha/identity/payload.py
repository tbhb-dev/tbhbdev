# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read typed values from hook payloads and JSONL records."""

import json
import os
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

type Json = dict[str, object]


def as_str(value: object) -> str | None:
    """The value if it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def as_dict(value: object) -> Json | None:
    """The value if it is a JSON object, else None."""
    if isinstance(value, dict):
        return cast("Json", value)  # pragma: no mutate -- a runtime no-op
    return None


def first_str(data: Json, *names: str) -> str | None:
    """The first of ``names`` whose value in ``data`` is a non-empty string."""
    for name in names:
        value = as_str(data.get(name))
        if value:
            return value
    return None


def parse_stdin(data: Json) -> tuple[str | None, str | None, str | None]:
    """Hook stdin -> (raw session ID, model, effort); each None if absent."""
    sid = first_str(
        data, "session_id", "sessionId", "conversationId", "conversation_id"
    )
    model = first_str(data, "model", "modelName", "to_model")
    effort = data.get("effort")
    nested = as_dict(effort)
    return sid, model, as_str(nested.get("level") if nested else effort)


def read_tail(path: Path, size: int) -> list[str] | None:
    """The lines in the last ``size`` bytes of a file, or None if unreadable."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            return handle.read().decode(errors="replace").splitlines()
    except OSError:
        return None


def parse_line(line: str) -> Json | None:
    """One JSON-lines record, or None if it is not a JSON object."""
    try:
        return as_dict(json.loads(line))
    except ValueError:
        return None


def rec_ts(rec: Json) -> float:
    """A stored record's timestamp (our own store: trusted to be a number)."""
    ts = rec.get("ts", 0)
    return cast("float", ts)  # pragma: no mutate -- a runtime no-op
