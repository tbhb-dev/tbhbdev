# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read commands and format guard decisions in the harness payload format."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tbhbdev.alpha.repo import Json


def command_of(data: Json) -> tuple[str, bool]:
    """(shell command, whether the payload is Antigravity's) from hook stdin."""
    call = data.get("toolCall")
    if isinstance(call, dict):
        args = call.get("args")
        text = args.get("CommandLine") if isinstance(args, dict) else None
        return (text if isinstance(text, str) else ""), True
    inputs = data.get("tool_input")
    text = inputs.get("command") if isinstance(inputs, dict) else None
    return (text if isinstance(text, str) else ""), False


def decision(message: str, *, antigravity: bool) -> Json:
    """The deny output in the harness's own contract."""
    if antigravity:
        return {"decision": "deny", "reason": message}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }
