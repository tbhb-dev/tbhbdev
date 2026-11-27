# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise the GitHub polling hook's payload contracts."""

import io
import json
import sys

import pytest

from tbhbdev.alpha.guard.gh_poll import command as guard
from tbhbdev.alpha.guard.gh_poll.decision import POLL_REASON
from tbhbdev.alpha.guard.gh_poll.token import UNSET_REASON
from tbhbdev.alpha.guard.git_hooks import PUSH_REASON

TOKEN = "OP_SERVICE_" + "ACCOUNT_TOKEN"


@pytest.mark.parametrize("agy", [False, True])
@pytest.mark.parametrize(
    ("command", "reason"),
    [("gh run list", POLL_REASON), (f"unset {TOKEN}", UNSET_REASON)],
)
def test_hook_contract(
    command: str,
    reason: str,
    *,
    agy: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = (
        {"toolCall": {"args": {"CommandLine": command}}}
        if agy
        else {"tool_input": {"command": command}}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    output = json.loads(capsys.readouterr().out)
    if agy:
        assert output == {"decision": "deny", "reason": reason}
    else:
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }


@pytest.mark.parametrize(
    "payload",
    ["invalid", "[]", "{}", '{"tool_input":{"command":"true"}}', ""],
)
def test_hook_allows_unreadable_or_allowed_input(
    payload: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_hook_crashes_fail_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(_text: str) -> None:
        raise RuntimeError

    monkeypatch.setattr(guard, "read_hook_stdin", lambda: {"tool_input": {}})
    monkeypatch.setattr(guard, "inspect", crash)
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_deep_nesting_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command = "eval " * 5000 + "gh run list"
    payload = json.dumps({"tool_input": {"command": command}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_poll_hook_denies_bypass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_input": {"command": "git commit --no-verify -m x"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_poll_hook_denies_force_push(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_input": {"command": "git push --force origin topic"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == PUSH_REASON
