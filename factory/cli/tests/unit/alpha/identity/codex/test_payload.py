# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity codex payload."""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st
from unit.alpha.identity.identity_examples import Json, patch_call

from tbhbdev.alpha.identity import models, records
from tbhbdev.alpha.identity.codex import payload as codex_payload

RAW_PATCH = (
    "*** Begin Patch\n*** Add File: a.txt\n+x\n*** Update File: dir/b.py\n@@\n-1\n+2\n"
    "*** Delete File: c\n*** Update File: d\n*** Move to: e\n*** End Patch"
)


def test_patch_paths_formats() -> None:
    expected = ["a.txt", "dir/b.py", "c", "d", "e"]
    assert codex_payload.patch_paths(RAW_PATCH) == expected
    js = (
        'const patch = "*** Begin Patch\\n*** Add File: codex.txt\\n+hi\\n'
        '*** End Patch";\nawait tools.apply_patch(patch);'
    )
    assert codex_payload.patch_paths(js) == ["codex.txt"]
    assert codex_payload.patch_paths(json.dumps({"input": RAW_PATCH})) == expected
    assert codex_payload.patch_paths("no patch here *** Add File: x") == []
    assert codex_payload.patch_paths(None) == []
    assert codex_payload.patch_paths(5) == []
    assert codex_payload.patch_paths(
        "*** Begin Patch\n*** Add File: x\n*** Add File: x\n"
    ) == ["x"]
    assert codex_payload.patch_paths("*** Begin Patch\n*** Add File:  spaced  \n") == [
        "spaced"
    ]
    assert codex_payload.patch_paths("*** Begin Patch\r\n*** Add File: w\r\n") == ["w"]
    assert codex_payload.patch_paths("*** Begin Patch\n*** Add File: tick`\n") == [
        "tick"
    ]
    assert codex_payload.patch_paths("*** Begin Patch\n*** Add File: at-end") == [
        "at-end"
    ]
    assert codex_payload.patch_paths("*** Begin Patch\n*** Move to: m'q") == ["m"]


@settings(database=None)
@given(
    st.lists(
        st.from_regex(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,15}", fullmatch=True),
        min_size=1,
        max_size=5,
        unique=True,
    )
)
def test_patch_paths_round_trip(paths: list[str]) -> None:
    body = "*** Begin Patch\n" + "".join(f"*** Add File: {p}\n+x\n" for p in paths)
    assert codex_payload.patch_paths(body + "*** End Patch") == paths
    assert codex_payload.patch_paths(json.dumps({"input": body})) == paths


def test_tool_call_paths() -> None:
    def call(**payload: object) -> Json:
        return {"type": "response_item", "payload": payload}

    assert codex_payload.tool_call_paths(patch_call("a.txt")) == ["a.txt"]
    assert (
        codex_payload.tool_call_paths(call(type="custom_tool_call", input=RAW_PATCH))[0]
        == "a.txt"
    )
    assert (
        codex_payload.tool_call_paths(call(type="function_call", arguments=RAW_PATCH))[
            0
        ]
        == "a.txt"
    )
    assert (
        codex_payload.tool_call_paths(
            call(type="function_call", input=3, arguments=RAW_PATCH)
        )[0]
        == "a.txt"
    )
    assert (
        codex_payload.tool_call_paths(call(type="function_call", input=3, arguments=4))
        == []
    )
    assert codex_payload.tool_call_paths(call(type="message", input=RAW_PATCH)) == []
    assert codex_payload.tool_call_paths(call(input=RAW_PATCH)) == []
    assert (
        codex_payload.tool_call_paths({"type": "response_item", "payload": "x"}) == []
    )
    assert codex_payload.tool_call_paths({"type": "response_item"}) == []
    assert (
        codex_payload.tool_call_paths(
            {"type": "event", "payload": call(type="function_call")["payload"]}
        )
        == []
    )
    other: Json = {
        "type": "event_msg",
        "payload": {"type": "custom_tool_call", "input": RAW_PATCH},
    }
    assert codex_payload.tool_call_paths(other) == []


ROOT_THREAD = "01a19ebf-adec-7210-8ba8-550921c2b924"
CHILD_THREAD = "01a19ebf-adec-7210-8ba8-550921c2b925"


def test_codex_ident_defaults() -> None:
    env: dict[str, str] = {}
    got = codex_payload.codex_ident(ROOT_THREAD, {}, (None, None), env)
    assert got == models.Ident(records.sha256_64(ROOT_THREAD), "codex")
    meta_: Json = {"id": CHILD_THREAD, "cli_version": "1.2.3", "source": "cli"}
    got = codex_payload.codex_ident(ROOT_THREAD, meta_, (None, None), env)
    assert got.subagent == records.sha256_64(CHILD_THREAD)
    assert (got.harness, got.surface) == ("codex:1.2.3", "cli")
    got = codex_payload.codex_ident(
        ROOT_THREAD, {"id": ROOT_THREAD}, (None, None), {"CODEX_VERSION": "7"}
    )
    assert (got.subagent, got.harness) == (None, "codex:7")
