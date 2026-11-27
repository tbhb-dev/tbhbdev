# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Record Claude hook events, shell commands, and tool edits."""

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.identity import commands as identity_commands
from tbhbdev.alpha.identity.claude_commands import (
    AGENT_ENV,
    finish_command,
    record_command,
)
from tbhbdev.alpha.identity.claude_model import main_model, real_model, transcript_model
from tbhbdev.alpha.identity.edits import write_edit
from tbhbdev.alpha.identity.launch import start_fields
from tbhbdev.alpha.identity.models import surface, version
from tbhbdev.alpha.identity.payload import Json, as_dict, as_str, first_str, parse_stdin
from tbhbdev.alpha.identity.sessions import load_record, write_record, write_user_record
from tbhbdev.alpha.identity.trailers import utc
from tbhbdev.alpha.identity.values import (
    CLAUDE,
    SLUGS,
    Ident,
    Seen,
    make_ident,
    sha256_64,
)
from tbhbdev.alpha.repo import hook_cwd

if TYPE_CHECKING:
    from collections.abc import Mapping

REWRITE_FLAG = "ASCM_AGENT_REWRITE"

CLAUDE_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# The Claude Code event after a /model switch, with the new model as to_model.
SWITCH = "PostModelSwitch"


def child_transcript(data: Json) -> str | None:
    """A Claude subagent's transcript path.

    ``agent_transcript_path`` (SubagentStop) is authoritative; otherwise the path
    derives from the main transcript:
    ``<main transcript sans .jsonl>/subagents/agent-<agent_id>.jsonl``.
    """
    explicit = as_str(data.get("agent_transcript_path"))
    transcript, agent_id = as_str(data.get("transcript_path")), data.get("agent_id")
    if explicit:
        return explicit
    if transcript and agent_id:
        return str(
            Path(transcript).with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        )
    return None


def rewrite_output(agent_hash: str, tool_input: Json) -> Json | None:
    """Optional comparison path: carry the executing agent into the command env."""
    cmd = tool_input.get("command")
    if not isinstance(cmd, str):
        return None
    updated = {**tool_input, "command": f"export {AGENT_ENV}={agent_hash}; {cmd}"}
    return {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}
    }


def record_child(data: Json, effort: str | None) -> str:
    """Record a Claude subagent from hook stdin; return its agent hash."""
    agent, root = sha256_64(data["agent_id"]), sha256_64(data["session_id"])
    transcript = child_transcript(data)
    cwd = hook_cwd(data)
    write_record(
        agent,
        {
            "harness": SLUGS[CLAUDE],
            "root": root,
            "agent_type": data.get("agent_type"),
            "model": transcript_model(transcript),
            "effort": effort,
            "transcript": transcript,
        },
        cwd,
        start_fields(agent, root, cwd),
    )
    return agent


def diff_paths(text: str) -> list[str]:
    """File paths named by a unified or git diff."""
    return re.findall(r"^\+\+\+ (?:b/)?(\S.*)$", text, re.MULTILINE) + re.findall(
        r"^diff --git a/.+ b/(.+)$", text, re.MULTILINE
    )


def walk_paths(node: object) -> list[str]:
    """Paths found anywhere in a nested diff structure of dicts, lists and text."""
    if isinstance(node, str):
        return diff_paths(node)
    if isinstance(node, list):
        items = cast("list[object]", node)  # pragma: no mutate -- a runtime no-op
        return [p for item in items for p in walk_paths(item)]
    fields = as_dict(node)
    if fields is None:
        return []
    out = [
        path
        for k in ("filePath", "file_path", "path")
        if (path := as_str(fields.get(k)))
    ]
    return out + [p for value in fields.values() for p in walk_paths(value)]


def edit_paths_from_bash(resp: object) -> list[str]:
    """Paths from ``tool_response.bashEditDiff`` (undocumented shape).

    Accepts a dict, a list or diff text.
    """
    diff = (as_dict(resp) or {}).get("bashEditDiff")
    if diff is None:
        return []
    return [p for p in dict.fromkeys(walk_paths(diff)) if p != "/dev/null"]


def claude_ident(data: Json, env: Mapping[str, str]) -> Ident:
    """The identity of the Claude agent (main or subagent) behind a tool hook."""
    root = sha256_64(data["session_id"])
    agent = data.get("agent_id")
    effort = parse_stdin(data)[2]
    rec = load_record(root, hook_cwd(data))
    if agent:
        model, sub = transcript_model(child_transcript(data)), sha256_64(agent)
    else:
        # The transcript tail can hold no assistant entry (after a large tool
        # result), so the main agent falls back to its session record.
        model = transcript_model(data.get("transcript_path")) or main_model(rec)
        sub = None
    ver = version(CLAUDE, env) or rec.get("version")
    return make_ident(CLAUDE, root, Seen(ver, model, effort, surface(CLAUDE, env)), sub)


def tool_input_of(data: Json) -> Json:
    """The hook's ``tool_input`` object, or an empty dict."""
    return as_dict(data.get("tool_input")) or {}


def write_bash_edits(
    data: Json, env: Mapping[str, str], cmd: object, cwd: Path | None
) -> None:
    """Edit receipts for the paths in a Bash result, unless git only moved them."""
    paths = edit_paths_from_bash(data.get("tool_response"))
    if paths and not identity_commands.moves_work_tree(cmd):
        ident = claude_ident(data, env)
        for path in paths:
            write_edit(ident, path, cwd)


def handle_claude_bash(
    event: str,
    data: Json,
    env: Mapping[str, str],
    agent: str | None,
    *,
    user_only: bool = False,
) -> None:
    """Bash command records, plus edit receipts when Claude reports a bash edit."""
    root, cwd = sha256_64(data["session_id"]), hook_cwd(data)
    tool_input = tool_input_of(data)
    cmd = tool_input.get("command")
    if event == "PreToolUse":
        record_command(root, agent, cmd, cwd)
        if env.get(REWRITE_FLAG) == "1" and agent:
            out = rewrite_output(agent, tool_input)
            if out:
                print(json.dumps(out))
        return
    response = data.get("tool_response")
    # A command moved to the background (run_in_background, or past its
    # timeout) is still running, and no later event says when it ends.
    if not (isinstance(response, dict) and response.get("backgroundTaskId")):
        finish_command(root, cmd, agent, cwd, user_only=user_only)
    if event == "PostToolUse" and not user_only:
        write_bash_edits(data, env, cmd, cwd)


def claude_main_record(data: Json, env: Mapping[str, str]) -> Json:
    """The main session's known fields, including a switch ahead of its reply."""
    # A switch is kept apart from the transcript's model, which trails it
    # until the next reply, so a later hook never overwrites it.
    switched = (
        real_model(data.get("to_model"))
        if data.get("hook_event_name") == SWITCH
        else None
    )
    rec = {
        "harness": SLUGS[CLAUDE],
        "version": version(CLAUDE, env),
        "model": transcript_model(data.get("transcript_path")),
        "effort": parse_stdin(data)[2],
        "transcript": as_str(data.get("transcript_path")),
        "payload_model": real_model(first_str(data, "model", "modelName")),
        "switch_model": switched,
        "switch_at": time.time() if switched else None,
    }
    return {k: v for k, v in rec.items() if v}


def finish_user_only(data: Json, env: Mapping[str, str], agent: str | None) -> None:
    """Finish the user-store record of a Bash command that has just ended."""
    event = data.get("hook_event_name")
    if (
        data.get("tool_name") == "Bash"
        and isinstance(event, str)
        and event in {"PostToolUse", "PostToolUseFailure"}
    ):
        handle_claude_bash(event, data, env, agent, user_only=True)


def handle_claude(
    data: Json, env: Mapping[str, str], *, user_only: bool = False
) -> None:
    """Record a Claude hook, or just its main session in the user's store."""
    event, tool = data.get("hook_event_name"), data.get("tool_name")
    if user_only and data.get("agent_id"):
        finish_user_only(data, env, sha256_64(data["agent_id"]))
        return
    effort = parse_stdin(data)[2]
    agent = None
    if data.get("agent_id"):
        agent = record_child(data, effort)
    else:
        rec = claude_main_record(data, env)
        root = sha256_64(data["session_id"])
        if user_only:
            write_user_record(root, rec, {"started_at": utc(time.time())})
            # A command begun outside any repository can end inside one, as
            # `git init` does, so its user record must still be finished.
            finish_user_only(data, env, None)
            return
        cwd = hook_cwd(data)
        write_record(root, rec, cwd, start_fields(root, root, cwd))
    if tool == "Bash" and event in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
        handle_claude_bash(event, data, env, agent)
    elif event == "PostToolUse" and tool in CLAUDE_EDIT_TOOLS:
        tool_input = tool_input_of(data)
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if file_path:
            write_edit(claude_ident(data, env), file_path, hook_cwd(data))
