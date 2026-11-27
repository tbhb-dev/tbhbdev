# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read fields common to harness hook payloads."""

from __future__ import annotations

from tbhbdev.alpha.identity.records import Json, as_dict, as_str, first_str


def parse_stdin(data: Json) -> tuple[str | None, str | None, str | None]:
    """Hook stdin -> (raw session ID, model, effort); each None if absent."""
    sid = first_str(
        data, "session_id", "sessionId", "conversationId", "conversation_id"
    )
    model = first_str(data, "model", "modelName", "to_model")
    effort = data.get("effort")
    nested = as_dict(effort)
    return sid, model, as_str(nested.get("level") if nested else effort)


def tool_input_of(data: Json) -> Json:
    """The hook's ``tool_input`` object, or an empty dict."""
    return as_dict(data.get("tool_input")) or {}
