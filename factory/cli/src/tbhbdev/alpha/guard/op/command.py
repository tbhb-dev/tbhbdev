# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Run the 1Password account guard on a harness payload."""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev import userconfig
from tbhbdev.alpha.guard.op.decision import allowed, message
from tbhbdev.alpha.guard.op.edits import writes
from tbhbdev.alpha.guard.op.file_calls import file_calls
from tbhbdev.alpha.guard.op.shell_calls import shell_calls
from tbhbdev.alpha.guard.payload import command_of, decision
from tbhbdev.alpha.identity.dispatcher import agy_tool
from tbhbdev.alpha.repo import read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class Check:
    """Remember finds before any config read or other fallible operation."""

    found: str = ""
    account: str = ""
    antigravity: bool = False

    def offenders(self, calls: Iterator[list[str]]) -> set[str]:
        bad: set[str] = set()
        for words in calls:
            self.found = " ".join(words)
            self.account = userconfig.string("op", "account")
            if not allowed(words, self.account):
                bad.add(self.found)
        return bad


def main() -> int:
    """Read a PreToolUse payload and deny new offending invocations."""
    check = Check()
    try:
        data = read_hook_stdin()
        text, check.antigravity = command_of(data)
        tool = agy_tool(data)[0]
        bad = (
            check.offenders(shell_calls(text))
            if tool in {"Bash", "shell", "run_command"}
            else set()
        )
        for path, before, after in writes(data):
            old = check.offenders(file_calls(path, before))
            bad |= check.offenders(file_calls(path, after)) - old
        if not bad:
            return 0
        reason = message(min(bad), check.account)
    except Exception:  # noqa: BLE001 -- allow only before finding an op call
        if not check.found:
            return 0
        reason = message(check.found, check.account)
    print(json.dumps(decision(reason, antigravity=check.antigravity)))
    return 0
