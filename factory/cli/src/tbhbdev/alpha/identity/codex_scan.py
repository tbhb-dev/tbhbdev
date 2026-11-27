# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Recover unrecorded edits from Codex rollouts."""

from pathlib import Path
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.identity.codex_rollout import (
    codex_ident,
    codex_threads,
    rollout_patch_edits,
)
from tbhbdev.alpha.identity.edits import write_edit
from tbhbdev.alpha.identity.payload import Json, as_str
from tbhbdev.alpha.identity.storage import Repo, locked, read_record, stores, write_json
from tbhbdev.alpha.identity.values import CODEX, Committer, hx, sha256_64

if TYPE_CHECKING:
    from collections.abc import Mapping


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
            mark = read_record(mark_path).get("offset", 0)
            offset = cast("int", mark)  # pragma: no mutate -- a runtime no-op
            edits, new = rollout_patch_edits(rollout, offset, replayed_lines(meta))
            if new != offset:
                write_json(mark_path, {"offset": new})
            if edits:
                ident = codex_ident(root_raw, meta, rollout, env)
                for path in dict.fromkeys(edits):
                    write_edit(ident, path, Path(as_str(meta.get("cwd")) or repo.top))


def refresh_codex(committer: Committer, repo: Repo, env: Mapping[str, str]) -> None:
    """Codex: turn rollout edits no hook recorded into receipts before using them."""
    if committer.key == CODEX:
        codex_scan(env["CODEX_SESSION_ID"], repo, env)
