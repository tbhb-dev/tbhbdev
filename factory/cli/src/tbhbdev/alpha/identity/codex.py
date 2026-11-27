# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Record Codex hook events and resolve the current thread identity."""

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.claude import tool_input_of
from tbhbdev.alpha.identity.codex_rollout import (
    codex_ident,
    codex_rollout,
    patch_paths,
    rollout_meta,
)
from tbhbdev.alpha.identity.edits import write_edit
from tbhbdev.alpha.identity.launch import start_fields
from tbhbdev.alpha.identity.models import provider_model
from tbhbdev.alpha.identity.payload import Json, as_str, parse_stdin
from tbhbdev.alpha.identity.sessions import write_record
from tbhbdev.alpha.identity.values import CODEX, Committer, Ident
from tbhbdev.alpha.repo import hook_cwd

if TYPE_CHECKING:
    from collections.abc import Mapping

CODEX_PATCH_TOOL = "apply_patch"

CODEX_START_EVENTS = {"SessionStart", "SubagentStart"}


def codex_hook_ident(root_raw: str, data: Json, env: Mapping[str, str]) -> Ident:
    """The identity of the Codex thread (root or subagent) behind a hook.

    The thread is ``agent_id`` or, for the root, ``session_id``. Its model is
    on stdin; version, surface and effort come from its own rollout, which
    ``transcript_path`` names.
    """
    agent = as_str(data.get("agent_id"))
    transcript = as_str(data.get("transcript_path"))
    rollout = Path(transcript) if transcript else None
    meta = {**(rollout_meta(rollout) if rollout else {}), "id": agent or root_raw}
    ident = codex_ident(root_raw, meta, rollout, env)
    model = provider_model(CODEX, parse_stdin(data)[1])
    return replace(ident, model=model or ident.model)


def handle_codex(data: Json, env: Mapping[str, str]) -> None:
    """Record a Codex hook event: thread records and apply_patch edit receipts.

    Anything else is ignored. Codex hooks have no ``CODEX_*`` environment, so
    identity comes from stdin alone.
    """
    root_raw, event = as_str(data.get("session_id")), data.get("hook_event_name")
    patching = event == "PostToolUse" and data.get("tool_name") == CODEX_PATCH_TOOL
    if not root_raw or not (patching or event in CODEX_START_EVENTS):
        return
    ident = codex_hook_ident(root_raw, data, env)
    cwd = hook_cwd(data)
    if not patching:
        write_record(
            ident.subagent or ident.root,
            {
                "harness": ident.harness,
                "root": ident.root,
                "agent_type": data.get("agent_type"),
                "model": ident.model,
                "effort": ident.effort,
                "surface": ident.surface,
                "transcript": data.get("transcript_path"),
            },
            cwd,
            start_fields(ident.subagent or ident.root, ident.root, cwd),
        )
        return
    for path in patch_paths(tool_input_of(data).get("command")):
        write_edit(ident, path, cwd, store=True)


def codex_committer(raw: str, env: Mapping[str, str]) -> Committer:
    """The Codex thread running git commit, plus the root session."""
    thread = env.get("CODEX_THREAD_ID") or raw
    thread_rollout, root_rollout = codex_rollout(thread, env), codex_rollout(raw, env)
    thread_meta = rollout_meta(thread_rollout) if thread_rollout else {}
    root_meta = rollout_meta(root_rollout) if root_rollout else {}
    return Committer(
        CODEX,
        codex_ident(raw, {**thread_meta, "id": thread}, thread_rollout, env),
        codex_ident(raw, {**root_meta, "id": raw}, root_rollout, env),
    )
