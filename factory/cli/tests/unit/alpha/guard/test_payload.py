# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise the harness payload and denial shapes."""

from tbhbdev.alpha.guard.payload import command_of, decision


def test_command_payloads() -> None:
    assert command_of({"tool_input": {"command": "git status"}}) == (
        "git status",
        False,
    )
    assert command_of({"toolCall": {"args": {"CommandLine": "gh pr view"}}}) == (
        "gh pr view",
        True,
    )
    assert command_of({"toolCall": {"args": "not an object"}}) == ("", True)


def test_denial_payloads() -> None:
    assert decision("blocked", antigravity=True) == {
        "decision": "deny",
        "reason": "blocked",
    }
    assert decision("blocked", antigravity=False) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "blocked",
        }
    }
