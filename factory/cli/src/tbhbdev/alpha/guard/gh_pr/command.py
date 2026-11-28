# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Run the pull request guard for a harness hook payload."""

import json

from tbhbdev.alpha.guard.gh_pr.decision import approval_message, repository_message
from tbhbdev.alpha.guard.payload import command_of, decision
from tbhbdev.alpha.repo import hook_cwd, in_tbhb_dev, read_hook_stdin


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        data = read_hook_stdin()
        text, antigravity = command_of(data)
        try:
            message = approval_message(text)
        except Exception:  # noqa: BLE001 -- still run the repository guard
            message = None
        if message:
            print(json.dumps(decision(message, antigravity=antigravity)))
            return 0
        message = repository_message(text, in_repository=in_tbhb_dev(hook_cwd(data)))
        if message:
            print(json.dumps(decision(message, antigravity=antigravity)))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
