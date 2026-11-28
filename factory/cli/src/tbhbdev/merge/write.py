# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""The one route for merge-queue writes: gh, then a ledger entry.

Every write the merge verbs make goes through ``gh_write``, which resolves gh
the same way for each verb and, when gh succeeds, appends one line to
``<user store>/merge-writes.jsonl``: when, which verb, which pull request and
the reason, if the verb takes one.
"""

import json
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.storage import locked, user_store
from tbhbdev.alpha.pull_request.tools import GH, find_tool, run

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Sequence

LEDGER = "merge-writes.jsonl"


def gh_write(
    verb: str,
    pull: tuple[str, int],
    argv: Sequence[str],
    *,
    stdin: str = "",
    reason: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a gh write on ``(repository, number)``; record it when it succeeds."""
    done = run([find_tool(GH), *argv], stdin=stdin)
    if done.returncode == 0:
        entry = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "verb": verb,
            "repo": pull[0],
            "pr": pull[1],
            "reason": reason,
        }
        try:
            record(entry)
        except OSError as exc:
            print(f"warning: the write ledger was not updated: {exc}", file=sys.stderr)
    return done


def record(entry: dict[str, object]) -> None:
    """Append one entry to the private ledger."""
    store = user_store()
    with locked(store, "merge-writes.lock"):
        fd = os.open(store / LEDGER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(entry) + "\n")
