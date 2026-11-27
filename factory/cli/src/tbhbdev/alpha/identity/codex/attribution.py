# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Resolve the identities of committing Codex threads."""

from __future__ import annotations

lazy from collections.abc import Mapping

from tbhbdev.alpha.identity.codex.payload import codex_ident
from tbhbdev.alpha.identity.codex.rollout import (
    codex_rollout,
    rollout_meta,
    rollout_turn_context,
)
from tbhbdev.alpha.identity.models import (
    CODEX,
    Committer,
)


def codex_committer(raw: str, env: Mapping[str, str]) -> Committer:
    """The Codex thread running git commit, plus the root session."""
    thread = env.get("CODEX_THREAD_ID") or raw
    thread_rollout, root_rollout = codex_rollout(thread, env), codex_rollout(raw, env)
    thread_meta = rollout_meta(thread_rollout) if thread_rollout else {}
    root_meta = rollout_meta(root_rollout) if root_rollout else {}
    return Committer(
        CODEX,
        codex_ident(
            raw,
            {**thread_meta, "id": thread},
            rollout_turn_context(thread_rollout),
            env,
        ),
        codex_ident(
            raw, {**root_meta, "id": raw}, rollout_turn_context(root_rollout), env
        ),
    )
