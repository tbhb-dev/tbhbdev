# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import io
import json
import sys
from datetime import UTC, datetime

import pytest

from tbhbdev.alpha.guard.sendmessage_clock import main

NOW = datetime(2026, 11, 26, 0, 2, tzinfo=UTC)


def test_denies_a_leading_stamp_six_minutes_away(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_input": {"message": "  [23:56Z] Started"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert main(now=NOW) == 0
    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Clock stamp(s)   [23:56Z are more than 5 minutes from now (00:02Z). "
                "Read `date -u` in this step and use that time, or drop the stamp. "
                "If the stamp is a quoted past event, rephrase it as an event name."
            ),
        }
    }


def test_allows_a_stamp_five_minutes_away_across_midnight(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_input": {"message": "23:57Z Started"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert main(now=NOW) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "stdin",
    ['{"tool_input":{"message":"Started at 23:56Z"}}', "{", "[]", '{"tool_input":[]}'],
)
def test_allows_later_stamps_and_bad_stdin(
    stdin: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    assert main(now=NOW) == 0
    assert capsys.readouterr().out == ""
