# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Resolve the current committer and assign credit for edits."""

from dataclasses import replace
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.antigravity import agy_committer
from tbhbdev.alpha.identity.claude_commit import claude_committer
from tbhbdev.alpha.identity.claude_model import transcript_model
from tbhbdev.alpha.identity.codex import codex_committer
from tbhbdev.alpha.identity.models import provider_model
from tbhbdev.alpha.identity.payload import Json, rec_ts
from tbhbdev.alpha.identity.sessions import load_record
from tbhbdev.alpha.identity.values import (
    CLAUDE,
    CODEX,
    Committer,
    Ident,
    detect,
    model_effort,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def committer_identity(
    env: Mapping[str, str], command_lines: list[str] | None = None
) -> Committer | None:
    """Identity of the agent running git commit, or None outside a harness."""
    detected = detect(env)
    if not detected:
        return None
    key, raw = detected
    if key == CLAUDE:
        return claude_committer(raw, env, command_lines)
    if key == CODEX:
        return codex_committer(raw, env)
    return agy_committer(raw, env)


def receipt_rank(receipt: Json, blobs: Mapping[str, str]) -> tuple[int, float]:
    """Sort key: receipts whose blob is the staged blob first, then newest."""
    blob = receipt.get("blob")
    is_staged_blob = bool(blob) and blob == blobs.get(str(receipt["path"]))
    return (0 if is_staged_blob else 1, -rec_ts(receipt))


def decide(
    committer: Committer,
    receipts: list[Json],
    staged: set[str],
    blobs: Mapping[str, str],
) -> tuple[list[Ident], list[Ident]]:
    """Decide the trailer roles: (Generated-by identities, Assisted-by identities).

    Pure. See the plan's section 10a, "Which key".
    """
    matched = [r for r in receipts if r["path"] in staged]
    if matched:
        # The last writer (blob equals the staged blob) first, then everyone else
        # who edited staged paths.
        matched.sort(key=lambda r: receipt_rank(r, blobs))
        gen = list(dict.fromkeys(Ident.from_receipt(r) for r in matched))
    else:
        gen = [committer.ident]  # fallback: no edit evidence on staged paths
    gen_agents = {i.agent_key() for i in gen}
    assisted = []
    if committer.ident.agent_key() not in gen_agents:
        assisted.append(committer.ident)
    if committer.ident.subagent and committer.root_ident.agent_key() not in gen_agents:
        assisted.append(committer.root_ident)
    return gen, assisted


def enrich(ident: Ident, committer: Committer) -> Ident:
    """Fill what the edit-time hook could not see.

    The harness version (the hook env lacks ``CLAUDE_CODE_EXECPATH``) comes from
    the committer of the same harness. A Claude child's model and effort come
    from its record, because the child transcript can trail the first
    PostToolUse. Surface is per session, and an edit hook env may not expose it
    (agy). A main agent's identity stored without a model or effort takes them
    from the committer's root identity when that is the same agent. Never
    inherits a model from another agent.
    """
    slug = ident.harness.partition(":")[0]
    committer_harness = committer.ident.harness
    same_harness = committer_harness.partition(":")[0] == slug
    if same_harness and ":" not in ident.harness and ":" in committer_harness:
        ident = replace(ident, harness=committer_harness)
    if same_harness and not ident.surface and committer.ident.surface:
        ident = replace(ident, surface=committer.ident.surface)
    if (
        ident.subagent
        and committer.key == CLAUDE
        and not (ident.model and ident.effort)
    ):
        child_rec = load_record(ident.subagent)
        if child_rec.get("root") == ident.root:
            model, effort = model_effort(
                CLAUDE,
                child_rec.get("model") or transcript_model(child_rec.get("transcript")),
                child_rec.get("effort"),
            )
            ident = replace(
                ident, model=ident.model or model, effort=ident.effort or effort
            )
    root_ident = committer.root_ident
    # The root identity has no subagent, so a match is the main agent itself.
    if committer.key == CLAUDE and ident.agent_key() == root_ident.agent_key():
        ident = replace(
            ident,
            model=ident.model or root_ident.model,
            effort=ident.effort or root_ident.effort,
        )
    return ident


def with_run_model(committer: Committer, configured_model: str | None) -> Committer:
    """Use the launcher's model when the root harness has no model record."""
    root = committer.root_ident
    if root.model or not (model := provider_model(committer.key, configured_model)):
        return committer
    updated = replace(root, model=model)
    return replace(
        committer,
        root_ident=updated,
        ident=committer.ident if committer.ident.subagent else updated,
    )
