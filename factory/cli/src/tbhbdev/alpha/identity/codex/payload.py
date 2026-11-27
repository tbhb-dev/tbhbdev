# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Parse Codex patch calls and rollout metadata."""

from __future__ import annotations

import re
lazy from collections.abc import Mapping

from tbhbdev.alpha.identity.models import (
    CODEX,
    Ident,
    Seen,
    make_ident,
    surface_ok,
    version,
)
from tbhbdev.alpha.identity.records import Json, as_dict, as_str, sha256_64

THREAD_ID = re.compile(r"[0-9a-fA-F-]{8,64}")


PATCH_END = r"(?:\\n|\\r|\n|\r|\"|'|`|$)"


PATCH_FILE = re.compile(rf"\*\*\* (?:Add|Update|Delete) File: (.+?){PATCH_END}")


PATCH_MOVE = re.compile(rf"\*\*\* Move to: (.+?){PATCH_END}")


def patch_paths(text: object) -> list[str]:
    """File paths from apply_patch text.

    Reads ``*** Add/Update/Delete File:`` and ``*** Move to:`` lines.

    The patch may sit raw in a custom tool call input, inside a JSON arguments
    string, or inside the JavaScript source of Codex's ``exec`` tool
    (``tools.apply_patch(patch)``), so newlines can be escaped.
    """
    if not isinstance(text, str) or "*** Begin Patch" not in text:
        return []
    found = (m.strip() for rx in (PATCH_FILE, PATCH_MOVE) for m in rx.findall(text))
    return list(dict.fromkeys(found))


def tool_call_paths(record: Json) -> list[str]:
    """apply_patch paths in one rollout line, counting only tool-call records."""
    payload = (
        as_dict(record.get("payload"))
        if record.get("type") == "response_item"
        else None
    )
    if payload is None:
        return []
    if payload.get("type") not in {
        "custom_tool_call",
        "function_call",
    }:
        return []
    body = payload.get("input")
    if not isinstance(body, str):
        body = payload.get("arguments")
    return patch_paths(body)


def replayed_lines(meta: Json) -> int:
    """How many leading rollout lines are a fork's replayed parent history."""
    # A fork's own history starts at this line; before it is the replayed parent.
    replayed = meta.get("subagent_history_start_ordinal")
    # Equivalent mutants: skipping 0 or 1 lines is the same, because line 0
    # is the session_meta and holds no patch, so `> 0` vs `> 1` and
    # `else 0` vs `else 1` cannot be told apart. The pragma has to sit on
    # the line mutmut mutates.
    fork = isinstance(replayed, int) and replayed > 0  # pragma: no mutate
    return replayed if fork else 0  # pragma: no mutate


CODEX_PATCH_TOOL = "apply_patch"


CODEX_START_EVENTS = {"SessionStart", "SubagentStart"}


def codex_ident(
    root_raw: str,
    meta: Json,
    context: tuple[str | None, str | None],
    env: Mapping[str, str],
) -> Ident:
    """The identity of a Codex thread within its root session."""
    tid = as_str(meta.get("id")) or root_raw
    model, effort = context
    seen = Seen(
        version(CODEX, env) or meta.get("cli_version"),
        model,
        effort,
        # Only plain-string sources (such as exec) count; objects are subagents.
        surface_ok(meta.get("source")),
    )
    return make_ident(
        CODEX, sha256_64(root_raw), seen, sha256_64(tid) if tid != root_raw else None
    )


def turn_context(record: Json) -> tuple[str | None, str | None] | None:
    """A rollout line's model and effort, if it contains a turn context."""
    if (
        record.get("type") == "turn_context"
        and (payload := as_dict(record.get("payload"))) is not None
    ):
        return as_str(payload.get("model")), as_str(payload.get("effort"))
    return None
