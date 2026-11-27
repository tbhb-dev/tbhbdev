# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read the process ancestry of a Claude command."""

from __future__ import annotations

import os
from pathlib import Path

from tbhbdev.alpha.process import process_row

ANCESTRY_LIMIT = 16


def ancestry(pid: int | None = None, limit: int = ANCESTRY_LIMIT) -> list[str]:
    """Command lines from this process up to and including the claude process."""
    out: list[str] = []
    current = str(pid or os.getpid())
    for _ in range(limit):
        row = process_row(current)
        if row is None:
            break
        ppid, cmd = row
        out.append(cmd)
        if Path(cmd.partition(" ")[0]).name == "claude" or ppid in {"", "0", "1"}:
            break
        current = ppid
    return out
