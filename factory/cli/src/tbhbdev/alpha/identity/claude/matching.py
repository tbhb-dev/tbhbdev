# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Match pending Claude commands against process command lines."""

from __future__ import annotations

from typing import cast

from tbhbdev.alpha.identity import commands as shell_commands
from tbhbdev.alpha.identity.records import AttributionError, Json, as_str

CMD_WINDOW = 2 * 3600


def rec_hashes(rec: Json) -> set[str]:
    """A stored command record's command hashes (our own store: trusted)."""
    hashes = rec.get("cmd", [])
    return set(cast("list[str]", hashes))  # pragma: no mutate -- a runtime no-op


def agent_for_commands(records: list[Json], command_lines: list[str]) -> str | None:
    """Match known pending command records to the process command lines."""
    hashes = {
        h
        for line in command_lines
        for cand in shell_commands.command_candidates(line)
        for h in shell_commands.cmd_hashes(cand)
    }
    agents = {as_str(rec.get("agent")) for rec in records if hashes & rec_hashes(rec)}
    if len(agents) == 1:
        return agents.pop()
    if any(as_str(rec.get("agent")) for rec in records):
        message = (
            "Attribution couldn't be resolved for a commit made while a subagent "
            "was active. Retry the commit from the main session after the "
            "subagent's command finishes."
        )
        raise AttributionError(message)
    return None
