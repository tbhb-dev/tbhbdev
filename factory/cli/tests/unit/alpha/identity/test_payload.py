# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity payload."""

from __future__ import annotations

from tbhbdev.alpha.identity import payload
lazy from tbhbdev.alpha.identity.records import Json


def test_parse_stdin_fixtures() -> None:
    main: Json = {"session_id": "session-raw", "effort": {"level": "high"}}
    assert payload.parse_stdin(main) == ("session-raw", None, "high")
    codex: Json = {
        "session_id": "01a19ebf-adec-7210-8ba8-550921c2b924",
        "model": "gpt-5.6-sol",
        "effort": "high",
    }
    assert payload.parse_stdin(codex) == (
        "01a19ebf-adec-7210-8ba8-550921c2b924",
        "gpt-5.6-sol",
        "high",
    )
    agy: Json = {"conversationId": "conv-raw", "modelName": "gemini-3.8-flash-low"}
    assert payload.parse_stdin(agy) == ("conv-raw", "gemini-3.8-flash-low", None)
    assert payload.parse_stdin({"sessionId": "s", "to_model": "m"}) == ("s", "m", None)
    assert payload.parse_stdin({"conversation_id": "c"}) == ("c", None, None)
    assert payload.parse_stdin({"effort": {"x": 1}}) == (None, None, None)
    assert payload.parse_stdin({"effort": {"level": "low"}})[2] == "low"
    assert payload.parse_stdin({}) == (None, None, None)


def test_tool_input_of() -> None:
    assert payload.tool_input_of({"tool_input": {"command": "x"}}) == {"command": "x"}
    assert payload.tool_input_of({"tool_input": "x"}) == {}
    assert payload.tool_input_of({}) == {}
