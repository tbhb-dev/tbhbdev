# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Select the active harness and resolve its committing identity."""

from __future__ import annotations

lazy from collections.abc import Mapping

from tbhbdev.alpha.identity.antigravity.session import agy_committer
from tbhbdev.alpha.identity.claude.attribution import claude_committer
from tbhbdev.alpha.identity.codex.attribution import codex_committer
from tbhbdev.alpha.identity.models import CLAUDE, CODEX, Committer, detect


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
