# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Run the operator script guard on a harness payload."""

import json
import os

from tbhbdev.alpha.guard.payload import command_of, decision
from tbhbdev.alpha.guard.scripts.filesystem import first_refusal
from tbhbdev.alpha.repo import read_hook_stdin


def main() -> int:
    """Read the payload on stdin; print a deny decision for a refused write."""
    try:
        data = read_hook_stdin()
        message = first_refusal(data, os.environ)
        if message:
            print(json.dumps(decision(message, antigravity=command_of(data)[1])))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
