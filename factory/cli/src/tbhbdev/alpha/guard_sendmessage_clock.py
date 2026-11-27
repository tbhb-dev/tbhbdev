# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Block a SendMessage with a leading stamp more than five minutes from UTC now."""

import json
import re
from contextlib import suppress
from datetime import UTC, datetime

from tbhbdev.alpha.guard_gh_pr import decision
from tbhbdev.alpha.repo import read_hook_stdin

LIMIT_MIN = 5
MINUTES_PER_DAY = 1440
STAMP = re.compile(r"\s*\[?([01]\d|2[0-3]):([0-5]\d)Z\b")


def main(*, now: datetime | None = None) -> int:
    """Read a hook event on stdin, optionally supplying the current UTC time."""
    data: dict[str, object] = {}
    with suppress(OSError):
        data = read_hook_stdin()
    inputs = data.get("tool_input")
    message = inputs.get("message") if isinstance(inputs, dict) else None
    if not isinstance(message, str):
        return 0

    now = now or datetime.now(UTC)
    stamp = STAMP.match(message)
    if stamp is None:
        return 0
    stamp_min = int(stamp[1]) * 60 + int(stamp[2])
    diff = abs(stamp_min - (now.hour * 60 + now.minute))
    if min(diff, MINUTES_PER_DAY - diff) > LIMIT_MIN:
        reason = (
            f"Clock stamp(s) {stamp[0]} are more than {LIMIT_MIN} minutes from now "
            f"({now:%H:%MZ}). "
            "Read `date -u` in this step and use that time, or drop the stamp. "
            "If the stamp is a quoted past event, rephrase it as an event name."
        )
        print(json.dumps(decision(reason, antigravity=False)))
    return 0
