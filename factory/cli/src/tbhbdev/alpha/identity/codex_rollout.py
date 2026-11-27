# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read Codex rollout metadata, models, and patch calls."""

import json
import re
import time
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.models import surface_ok, version
from tbhbdev.alpha.identity.payload import Json, as_dict, as_str, parse_line, read_tail
from tbhbdev.alpha.identity.values import CODEX, Ident, Seen, make_ident, sha256_64

if TYPE_CHECKING:
    from collections.abc import Mapping

THREAD_ID = re.compile(r"[0-9a-fA-F-]{8,64}")

CODEX_TAIL = 1024 * 1024

CODEX_HEAD = 1024 * 1024

CODEX_MAX_AGE = 3 * 86400


def codex_home(env: Mapping[str, str]) -> Path:
    """The Codex home directory."""
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex")


def codex_rollout(thread_id: str, env: Mapping[str, str]) -> Path | None:
    """The newest ``<home>/sessions/Y/M/D/rollout-<ts>-<thread>.jsonl`` (read only)."""
    if not THREAD_ID.fullmatch(thread_id):
        return None
    hits = list(codex_home(env).glob(f"sessions/*/*/*/rollout-*-{thread_id}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def rollout_turn_context(path: Path) -> tuple[str | None, str | None]:
    """(model, effort) from the tail, falling back to the first turn context."""
    for line in reversed(read_tail(path, CODEX_TAIL) or []):
        record = parse_line(line) or {}
        payload = as_dict(record.get("payload"))
        if record.get("type") == "turn_context" and payload is not None:
            return as_str(payload.get("model")), as_str(payload.get("effort"))
    try:
        with path.open("rb") as handle:
            remaining = CODEX_HEAD
            while remaining:
                raw = handle.readline(remaining)
                if not raw:
                    break
                remaining -= len(raw)
                record = parse_line(raw.decode(errors="replace")) or {}
                payload = as_dict(record.get("payload"))
                if record.get("type") == "turn_context" and payload is not None:
                    return as_str(payload.get("model")), as_str(payload.get("effort"))
    except OSError:
        pass
    return None, None


def rollout_meta(path: Path) -> Json:
    """The ``session_meta`` payload on a rollout's first line, or an empty dict."""
    try:
        with path.open() as handle:
            first = parse_line(handle.readline()) or {}
    except OSError:
        return {}
    payload = first.get("payload") if first.get("type") == "session_meta" else None
    return as_dict(payload) or {}


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
        "local_shell_call",
    }:
        return []
    body = payload.get("input")
    if not isinstance(body, str):
        body = payload.get("arguments")
    return patch_paths(
        body if isinstance(body, str) else json.dumps(payload.get("action"))
    )


def rollout_patch_edits(
    path: Path, offset: int = 0, skip_lines: int = 0
) -> tuple[list[str], int]:
    """(paths in order, new offset) for ``apply_patch`` lines after ``offset``.

    Only complete lines count.

    A forked Codex thread replays its parent's history first, including the
    parent's ``apply_patch`` calls. ``skip_lines`` is the length of that
    replayed prefix: reading starts after it, whatever ``offset`` says.
    """
    try:
        with path.open("rb") as handle:
            replayed = len(b"".join(islice(handle, skip_lines)))
            start = max(offset, replayed)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1
    edits: list[str] = []
    for line in data[:end].decode(errors="replace").splitlines():
        if "Begin Patch" not in line:
            continue
        record = parse_line(line)
        if record:
            edits.extend(tool_call_paths(record))
    return edits, start + end


def codex_threads(root_raw: str, env: Mapping[str, str]) -> list[tuple[Path, Json]]:
    """(rollout path, meta) for the root session's rollouts: root and child threads."""
    out = []
    now = time.time()
    for path in codex_home(env).glob("sessions/*/*/*/rollout-*.jsonl"):
        try:
            if now - path.stat().st_mtime > CODEX_MAX_AGE:
                continue
        except OSError:
            continue
        meta = rollout_meta(path)
        if root_raw in {meta.get("session_id"), meta.get("id")}:
            out.append((path, meta))
    return out


def codex_ident(
    root_raw: str, meta: Json, rollout: Path | None, env: Mapping[str, str]
) -> Ident:
    """The identity of a Codex thread within its root session."""
    tid = as_str(meta.get("id")) or root_raw
    model, effort = rollout_turn_context(rollout) if rollout else (None, None)
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
