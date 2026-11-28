# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import io
import json
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.guard.ops_no_lazy_stop import main

if TYPE_CHECKING:
    from pathlib import Path


def test_blocks_a_verification_qualifier(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"last_assistant_message": "The deployment is Unverified."}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "decision": "block",
        "reason": (
            'You wrote "Unverified". Before you stop: run the read-only check that '
            "settles it (a gh query, a file read, an ssh grep, a dry run) and state "
            "the result with its evidence. Only keep the label if the check is "
            "impossible from here (needs tbhb, a credential, or a change), and then "
            "say which."
        ),
    }


@pytest.mark.parametrize(
    "stdin",
    [
        '{"last_assistant_message":"The deployment passed."}',
        '{"stop_hook_active":true,"last_assistant_message":"Still unverified."}',
        '{"last_assistant_message":42}',
        "{",
        "[]",
    ],
)
def test_allows_clean_replies_active_stop_hooks_and_bad_stdin(
    stdin: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    assert main() == 0
    assert capsys.readouterr().out == ""


def test_uses_latest_assistant_message_from_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "assistant", "message": {"content": [{"text": "The build passed."}]}},
        {
            "type": "assistant",
            "message": {
                "content": [{"text": "I haven't "}, "ignored", {"text": "checked."}]
            },
        },
        {"type": "user", "message": {"content": [{"text": "Okay."}]}},
    ]
    transcript.write_text("\n".join(map(json.dumps, entries)), encoding="utf-8")
    payload = {"transcript_path": str(transcript)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "block"
    assert output["reason"].startswith('You wrote "haven\'t checked".')


@pytest.mark.parametrize("contents", [None, "", "{"])
def test_allows_a_transcript_without_a_readable_message(
    contents: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    if contents is not None:
        transcript.write_text(contents, encoding="utf-8")
    payload = {"transcript_path": str(transcript)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert main() == 0
    assert capsys.readouterr().out == ""
