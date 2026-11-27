# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Resolve and store Antigravity sessions and their edit receipts."""

from __future__ import annotations

import json
from pathlib import Path
lazy from collections.abc import Mapping

from tbhbdev.alpha.identity import sessions, storage
from tbhbdev.alpha.identity.antigravity.payload import (
    AGY_EDIT_TOOLS,
    agy_edit_path,
    agy_tool,
)
from tbhbdev.alpha.identity.models import (
    ANTIGRAVITY,
    SLUGS,
    Committer,
    Ident,
    Seen,
    make_ident,
    surface,
    version,
)
from tbhbdev.alpha.identity.receipts import write_edit
from tbhbdev.alpha.identity.records import Json, as_str, hx, sha256_64, without
from tbhbdev.alpha.identity.sessions import load_record, start_fields
from tbhbdev.alpha.identity.storage import locked, state_dir, write_json
from tbhbdev.alpha.repo import hook_cwd


def agy_parent(brain_root: Path, cid: str) -> str | None:
    """Parent conversation ID of an agy child, from the child's own brain dir.

    The first ``invoke_subagent`` message addressed to the child names it.
    """
    messages_dir = brain_root / cid / ".system_generated" / "messages"
    try:
        paths = sorted(messages_dir.iterdir())
    except OSError:
        return None
    for path in paths:
        if path.suffix != ".json" or path.name == "read.json":
            continue
        try:
            message = json.loads(path.read_text())
            tool = message["sourceMetadata"]["tool"]
            if (
                message.get("recipient") == cid
                and tool["toolCall"]["name"] == "invoke_subagent"
            ):
                parent = message.get("sender") or tool.get("conversationId")
                return parent if parent and parent != cid else None
        except OSError, ValueError, KeyError, TypeError:
            continue
    return None


def agy_root(brain_root: Path, cid: str, max_depth: int = 8) -> str:
    """Walk parent links up to the root conversation (cycle- and depth-safe)."""
    seen, current = {cid}, cid
    for _ in range(max_depth):
        parent = agy_parent(brain_root, current)
        if not parent or parent in seen:
            break
        seen.add(parent)
        current = parent
    return current


def agy_brain_root(rec: Json, env: Mapping[str, str]) -> Path | None:
    """The agy ``brain`` directory: recorded, from app data, or the default."""
    app_data = env.get("ANTIGRAVITY_APP_DATA_DIR")
    candidates = (
        Path(brain) if (brain := as_str(rec.get("brain_root"))) else None,
        Path(app_data) / "brain" if app_data else None,
        Path("~/.gemini/antigravity-cli/brain").expanduser(),
    )
    return next((c for c in candidates if c and c.is_dir()), None)


def agy_ident(
    cid: str, rec: Json, env: Mapping[str, str], brain: Path | None = None
) -> Ident:
    """The identity of an agy conversation, resolving its root through ``brain``."""
    root_raw = agy_root(brain, cid) if brain else cid
    seen = Seen(
        version(ANTIGRAVITY, env),
        rec.get("model"),
        rec.get("effort"),
        surface(ANTIGRAVITY, env),
    )
    return make_ident(
        ANTIGRAVITY,
        sha256_64(root_raw),
        seen,
        sha256_64(cid) if root_raw != cid else None,
    )


def record_agy(sid: str, data: Json, rec: Json, env: Mapping[str, str]) -> None:
    """agy: write an edit receipt for a successful edit tool call."""
    tool, args = agy_tool(data)
    # agy stdin has no event name; only PostToolUse carries an `error` key
    # (empty on success).
    if tool in AGY_EDIT_TOOLS and "error" in data and not data["error"]:
        path = agy_edit_path(args)
        if path:
            write_edit(
                agy_ident(sid, rec, env, agy_brain_root(rec, env)),
                path,
                hook_cwd(data),
            )


def save_agy_session(
    harness: str,
    sid: str,
    data: Json,
    seen: tuple[str | None, str | None],
    env: Mapping[str, str],
) -> Json | None:
    """Write an agy session's record to the clone and the user stores.

    Nothing is written, and None returned, outside a worktree.
    """
    model, effort = seen
    state = state_dir(hook_cwd(data))
    if not state:
        return None
    path = state / "sessions" / f"{hx(sha256_64(sid))}.json"
    # The executing conversation can be a subagent: only its root claims the launch.
    artifacts = as_str(data.get("artifactDirectoryPath"))
    brain = (
        Path(artifacts).parent
        if artifacts
        else agy_brain_root(storage.read_record(path), env)
    )
    root = sha256_64(agy_root(brain, sid) if brain else sid)
    once = start_fields(sha256_64(sid), root, hook_cwd(data))
    with locked(state):
        rec = storage.read_record(path)
        rec.update({k: v for k, v in once.items() if v and k not in rec})
        rec["harness"] = SLUGS[harness]
        if model and not model.startswith("<"):
            rec["model"] = model
        if effort:
            rec["effort"] = effort
        brain = as_str(data.get("artifactDirectoryPath"))
        if brain:
            rec["brain_root"] = str(Path(brain).parent)
        write_json(path, rec)
    fixed = {k: rec.get(k) for k in ("started_at", "launch")}
    sessions.write_user_record(sha256_64(sid), without(rec, fixed), fixed)
    return rec


def agy_committer(raw: str, env: Mapping[str, str]) -> Committer:
    """The agy conversation running git commit, plus its root conversation."""
    rec = load_record(sha256_64(raw))
    brain = agy_brain_root(rec, env)
    ident = agy_ident(raw, rec, env, brain)
    root_raw = agy_root(brain, raw) if brain else raw
    if root_raw == raw:
        return Committer(ANTIGRAVITY, ident, ident)
    root_ident = agy_ident(root_raw, load_record(sha256_64(root_raw)), env)
    return Committer(ANTIGRAVITY, ident, root_ident)
