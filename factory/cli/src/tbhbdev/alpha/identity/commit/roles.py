# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Assign trailer credit using the committer and staged edit receipts."""

from __future__ import annotations

from dataclasses import replace
lazy from collections.abc import Mapping
lazy from pathlib import Path

from tbhbdev.alpha.identity.models import (
    CLAUDE,
    Committer,
    Ident,
    model_effort,
    provider_model,
)
from tbhbdev.alpha.identity.records import Json, rec_ts


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


def enrich_ident(
    ident: Ident,
    committer: Committer,
    child_model: object = None,
    child_effort: object = None,
) -> Ident:
    """Fill missing attributes from the committer and the child's known model."""
    slug = ident.harness.partition(":")[0]
    committer_harness = committer.ident.harness
    same_harness = committer_harness.partition(":")[0] == slug
    if same_harness and ":" not in ident.harness and ":" in committer_harness:
        ident = replace(ident, harness=committer_harness)
    if same_harness and not ident.surface and committer.ident.surface:
        ident = replace(ident, surface=committer.ident.surface)
    if child_model is not None or child_effort is not None:
        model, effort = model_effort(CLAUDE, child_model, child_effort)
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


def pick_roles(
    committer: Committer, *, edited: bool
) -> tuple[list[Ident], list[Ident]]:
    """A picker wrote none of the change: it assists when it edited, else no one."""
    return [], [committer.ident] if edited else []


def committing(
    edits: list[tuple[Path, Json]], staged: set[str], unstaged: Mapping[str, str]
) -> list[tuple[Path, Json]]:
    """The receipts this commit contains: for a staged path, and not still unstaged.

    A receipt whose blob is exactly the path's unstaged work-tree content records
    an edit made after the path was staged. That edit is not in this commit, so
    it waits for the commit that stages it.
    """
    return [
        (path, rec)
        for path, rec in edits
        if rec["path"] in staged
        and not (rec["path"] in unstaged and rec.get("blob") == unstaged[rec["path"]])
    ]


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
