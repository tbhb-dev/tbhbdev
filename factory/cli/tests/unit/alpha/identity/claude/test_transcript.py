# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity claude transcript."""

from __future__ import annotations

from unit.alpha.identity.identity_examples import EARLIER, EARLIER_TS, OPUS

from tbhbdev.alpha.identity.claude import transcript as claude_transcript
lazy from tbhbdev.alpha.identity.records import Json


def test_child_transcript_is_derived() -> None:
    main: Json = {"transcript_path": "/nonexistent/session-raw.jsonl"}
    child: Json = {**main, "agent_id": "agent-raw"}
    assert claude_transcript.child_transcript(child) == (
        "/nonexistent/session-raw/subagents/agent-agent-raw.jsonl"
    )
    assert claude_transcript.child_transcript(main) is None
    assert claude_transcript.child_transcript({"agent_id": "a"}) is None
    assert claude_transcript.child_transcript({"transcript_path": "/t.jsonl"}) is None


def test_real_model() -> None:
    assert claude_transcript.real_model(OPUS) == OPUS
    assert claude_transcript.real_model("<synthetic>") is None
    assert claude_transcript.real_model("<other>") is None
    assert claude_transcript.real_model("") is None
    assert claude_transcript.real_model(None) is None
    assert claude_transcript.real_model(3) is None


def test_entry_time() -> None:
    assert claude_transcript.entry_time(EARLIER) == EARLIER_TS
    assert (
        claude_transcript.entry_time("2026-11-06T10:00:00.500+00:00")
        == EARLIER_TS + 0.5
    )
    assert claude_transcript.entry_time(None) == 0.0
    assert claude_transcript.entry_time("yesterday") == 0.0
    assert claude_transcript.entry_time("") == 0.0
