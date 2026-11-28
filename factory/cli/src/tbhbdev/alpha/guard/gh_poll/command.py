# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Run the GitHub polling and Git hook guards on a harness payload."""

import json
from contextlib import suppress

from tbhbdev.alpha.guard.gh_poll.decision import inspect
from tbhbdev.alpha.guard.git_hooks import verdict
from tbhbdev.alpha.guard.payload import command_of, decision
from tbhbdev.alpha.repo import read_hook_stdin


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    with suppress(Exception):  # fail open on any crash
        data = read_hook_stdin()
        text, antigravity = command_of(data)
        message = inspect(text)[0] or verdict(text)
        if message:
            print(json.dumps(decision(message, antigravity=antigravity)))
    return 0
