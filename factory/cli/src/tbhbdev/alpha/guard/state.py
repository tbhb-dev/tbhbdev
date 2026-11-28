# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Print the session rule and refuse agent sign-off and co-author trailers."""

import sys

from tbhbdev import userconfig
from tbhbdev.alpha.guard.trailers import pre_tool_reason
from tbhbdev.alpha.repo import Json, read_hook_stdin

TABLE = "harness"


def run(data: Json) -> int:
    """Handle one hook payload; the exit status."""
    if data.get("hook_event_name") == "SessionStart":
        rule = userconfig.optional_string(TABLE, "rule")
        if rule:
            print(rule)
        return 0
    reason = pre_tool_reason(data)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


def main() -> int:
    """Read the payload on stdin and run it; never fail for a crash or bad config."""
    try:
        return run(read_hook_stdin())
    except userconfig.ConfigError as exc:
        print(f"state-guard: ignoring the config: {exc}", file=sys.stderr)
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
