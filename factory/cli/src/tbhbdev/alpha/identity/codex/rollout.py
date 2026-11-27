# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read Codex rollouts and turn their patch calls into edit receipts."""

from __future__ import annotations

import time
from itertools import islice
from pathlib import Path
from typing import cast
lazy from collections.abc import Mapping

from tbhbdev.alpha.identity import storage
from tbhbdev.alpha.identity.codex.payload import (
    THREAD_ID,
    codex_ident,
    replayed_lines,
    tool_call_paths,
    turn_context,
)
from tbhbdev.alpha.identity.models import CODEX, Committer
from tbhbdev.alpha.identity.receipts import write_edit
from tbhbdev.alpha.identity.records import (
    Json,
    as_dict,
    as_str,
    hx,
    parse_line,
    sha256_64,
)
from tbhbdev.alpha.identity.storage import Repo, locked, read_tail, stores, write_json

CODEX_TAIL = 1024 * 1024


CODEX_HEAD = 1024 * 1024


CODEX_MAX_AGE = 3 * 86400


def codex_home(env: Mapping[str, str]) -> Path:
    """The Codex home directory."""
    home = env.get("CODEX_HOME")
    return Path(home) if home else Path.home() / ".codex"


def codex_rollout(thread_id: str, env: Mapping[str, str]) -> Path | None:
    """The newest ``<home>/sessions/Y/M/D/rollout-<ts>-<thread>.jsonl`` (read only)."""
    if not THREAD_ID.fullmatch(thread_id):
        return None
    hits = list(codex_home(env).glob(f"sessions/*/*/*/rollout-*-{thread_id}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def rollout_turn_context(path: Path | None) -> tuple[str | None, str | None]:
    """(model, effort) from the tail, falling back to the first turn context."""
    if path is None:
        return None, None
    for line in reversed(read_tail(path, CODEX_TAIL) or []):
        if (context := turn_context(parse_line(line) or {})) is not None:
            return context
    try:
        with path.open("rb") as handle:
            remaining = CODEX_HEAD
            while remaining:
                raw = handle.readline(remaining)
                if not raw:
                    break
                remaining -= len(raw)
                if (
                    context := turn_context(
                        parse_line(raw.decode(errors="replace")) or {}
                    )
                ) is not None:
                    return context
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


def codex_scan(root_raw: str, repo: Repo, env: Mapping[str, str]) -> None:
    """Turn new apply_patch calls in this root's rollouts into edit receipts.

    A thread with a hook record (SessionStart/SubagentStart) is skipped: its
    PostToolUse hook already wrote its receipts with the edit-time blob.

    The per-rollout byte offset (``marks/``) advances here so a later commit
    never re-reads them. The receipts' ``consumed`` flag (set by post-commit)
    is what stops double credit, so an aborted commit loses nothing. The scan
    holds the lock, so two concurrent scans never turn one patch into two
    receipts.
    """
    known = stores(repo.top)
    with locked(repo.shared, "marks.lock"):
        for rollout, meta in codex_threads(root_raw, env):
            name = f"{hx(sha256_64(as_str(meta.get('id')) or root_raw))}.json"
            if any((store / "sessions" / name).exists() for store in known):
                continue
            mark_path = repo.shared / "marks" / name
            mark = storage.read_record(mark_path).get("offset", 0)
            offset = cast("int", mark)  # pragma: no mutate -- a runtime no-op
            edits, new = rollout_patch_edits(rollout, offset, replayed_lines(meta))
            if new != offset:
                write_json(mark_path, {"offset": new})
            if edits:
                ident = codex_ident(root_raw, meta, rollout_turn_context(rollout), env)
                for path in dict.fromkeys(edits):
                    write_edit(ident, path, Path(as_str(meta.get("cwd")) or repo.top))


def refresh_codex(committer: Committer, repo: Repo, env: Mapping[str, str]) -> None:
    """Codex: turn rollout edits no hook recorded into receipts before using them."""
    if committer.key == CODEX:
        codex_scan(env["CODEX_SESSION_ID"], repo, env)
