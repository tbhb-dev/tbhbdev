# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity antigravity payload."""

from __future__ import annotations

import json

from tbhbdev.alpha.identity.antigravity import payload as antigravity_payload


def test_agy_tool_and_edit_path() -> None:
    assert antigravity_payload.agy_tool(
        {"toolCall": {"name": "w", "args": {"a": 1}}}
    ) == ("w", {"a": 1})
    assert antigravity_payload.agy_tool({"toolName": "w", "toolInput": "x"}) == (
        "w",
        "x",
    )
    assert antigravity_payload.agy_tool({"tool_name": "w"}) == ("w", None)
    assert antigravity_payload.agy_tool({"toolCall": "x", "tool_name": "w"}) == (
        "w",
        None,
    )
    assert antigravity_payload.agy_tool(
        {"toolCall": {"name": "n"}, "toolName": "x"}
    ) == ("n", None)
    assert antigravity_payload.agy_tool({}) == (None, None)
    assert antigravity_payload.agy_edit_path({"Path": "a"}) == "a"
    assert antigravity_payload.agy_edit_path(json.dumps({"TargetFile": "t"})) == "t"
    assert antigravity_payload.agy_edit_path("{") is None
    assert antigravity_payload.agy_edit_path("[1]") is None
    assert antigravity_payload.agy_edit_path(None) is None
    assert antigravity_payload.agy_edit_path({"Path": "", "path": "p"}) == "p"
    assert antigravity_payload.agy_edit_path({"other": "x"}) is None
    for key in antigravity_payload.AGY_PATH_KEYS:
        assert antigravity_payload.agy_edit_path({key: "k"}) == "k"
