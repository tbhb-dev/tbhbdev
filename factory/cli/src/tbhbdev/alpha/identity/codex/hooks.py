# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Record Codex session and edit hook events."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
lazy from collections.abc import Mapping

from tbhbdev.alpha.identity.codex.payload import (
    CODEX_PATCH_TOOL,
    CODEX_START_EVENTS,
    codex_ident,
    patch_paths,
)
from tbhbdev.alpha.identity.codex.rollout import rollout_meta, rollout_turn_context
from tbhbdev.alpha.identity.models import CODEX, Ident, provider_model
from tbhbdev.alpha.identity.payload import parse_stdin, tool_input_of
from tbhbdev.alpha.identity.receipts import write_edit
from tbhbdev.alpha.identity.records import Json, as_str
from tbhbdev.alpha.identity.sessions import start_fields, write_record
from tbhbdev.alpha.repo import hook_cwd


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
    ident = codex_ident(root_raw, meta, rollout_turn_context(rollout), env)
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
