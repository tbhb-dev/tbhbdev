# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Block a stop when the latest assistant message uses a verification qualifier."""

import json
import re
from contextlib import suppress
from pathlib import Path
from typing import cast

from tbhbdev.alpha.repo import read_hook_stdin

PAT = re.compile(
    r"\bunverified\b|\bnot verified\b|\bhaven'?t (?:checked|verified|read)\b|"
    r"\bI (?:did not|didn'?t) (?:check|verify|read)\b|\bnot checked\b|"
    r"\bI don'?t know whether\b|\bI'?m (?:still )?assuming\b|\bI assumed\b|"
    r"\bI (?:can'?t|cannot) (?:verify|confirm|read)\b|"
    r"\bnot confirmed\b|\bunconfirmed\b",
    re.IGNORECASE,
)


def transcript_message(path: object) -> str:
    """Read the most recent assistant message from the transcript, if available."""
    if isinstance(path, str):
        with suppress(
            AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError
        ):
            with Path(path).open(encoding="utf-8") as transcript:
                lines = [json.loads(line) for line in transcript]
            for entry in reversed(lines):
                if entry.get("type") == "assistant":
                    content: list[object] = entry["message"]["content"]
                    return "".join(
                        cast("str", block.get("text", ""))
                        for block in content
                        if isinstance(block, dict)
                    )
    return ""


def main() -> int:
    """Read a Stop event on stdin and print a block decision on a phrase match."""
    event: dict[str, object] = {}
    with suppress(OSError):
        event = read_hook_stdin()
    if event.get("stop_hook_active"):
        return 0
    message = event.get("last_assistant_message") or transcript_message(
        event.get("transcript_path")
    )
    hit = PAT.search(message) if isinstance(message, str) else None
    if hit:
        reason = (
            f'You wrote "{hit[0]}". Before you stop: run the read-only check that '
            "settles it (a gh query, a file read, an ssh grep, a dry run) and state "
            "the result with its evidence. Only keep the label if the check is "
            "impossible from here (needs tbhb, a credential, or a change), and then "
            "say which."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0
