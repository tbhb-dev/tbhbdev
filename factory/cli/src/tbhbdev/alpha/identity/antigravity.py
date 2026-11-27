# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Record Antigravity sessions and resolve their conversation identities."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.edits import write_edit
from tbhbdev.alpha.identity.launch import start_fields
from tbhbdev.alpha.identity.models import surface, version
from tbhbdev.alpha.identity.payload import Json, as_dict, as_str
from tbhbdev.alpha.identity.sessions import load_record, write_user_record
from tbhbdev.alpha.identity.storage import locked, read_record, state_dir, write_json
from tbhbdev.alpha.identity.values import (
    ANTIGRAVITY,
    SLUGS,
    Committer,
    Ident,
    Seen,
    hx,
    make_ident,
    sha256_64,
)
from tbhbdev.alpha.repo import hook_cwd

if TYPE_CHECKING:
    from collections.abc import Mapping

# agy 1.4.12 -p (verified live) uses write_file and edit_file with the path in
# args.Path. The other names are the documented IDE-era tools, kept in case
# another mode uses them.
AGY_EDIT_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}

AGY_PATH_KEYS = (
    "Path",
    "TargetFile",
    "AbsolutePath",
    "target_file",
    "file_path",
    "filePath",
    "path",
)


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


def agy_tool(data: Json) -> tuple[object, object]:
    """(tool name, args) from agy hook stdin."""
    call = as_dict(data.get("toolCall")) or {}
    return (
        call.get("name") or data.get("toolName") or data.get("tool_name"),
        call.get("args") or data.get("toolInput"),
    )


def agy_edit_path(args: object) -> str | None:
    """The edited file's path from an agy tool call's args (dict or JSON text)."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    fields = as_dict(args)
    if fields is None:
        return None
    return next((p for key in AGY_PATH_KEYS if (p := as_str(fields.get(key)))), None)


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


def without(rec: Json, drop: Mapping[str, object]) -> Json:
    """A record without the keys of ``drop``."""
    return {k: v for k, v in rec.items() if k not in drop}


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
        Path(artifacts).parent if artifacts else agy_brain_root(read_record(path), env)
    )
    root = sha256_64(agy_root(brain, sid) if brain else sid)
    once = start_fields(sha256_64(sid), root, hook_cwd(data))
    with locked(state):
        rec = read_record(path)
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
    write_user_record(sha256_64(sid), without(rec, fixed), fixed)
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
