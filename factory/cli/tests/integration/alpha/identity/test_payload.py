# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity payload tests."""

import json
from typing import TYPE_CHECKING

import pytest
from integration.alpha.identity.identity_test_support import (
    patch_call,
    patch_paths_of,
    stdin_fixture,
    turn,
)

from tbhbdev.alpha.identity import claude_commands, codex_rollout, payload

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.identity.payload import Json


@pytest.mark.parametrize(
    ("value", "expected"),
    [("x", "x"), ("", None), (None, None), (3, None), (["x"], None)],
)
def test_as_str(value: object, expected: str | None) -> None:
    assert payload.as_str(value) == expected


def test_as_dict() -> None:
    assert payload.as_dict({"a": 1}) == {"a": 1}
    assert payload.as_dict({}) == {}
    assert payload.as_dict([1]) is None
    assert payload.as_dict("x") is None


def test_first_str_takes_first_nonempty_string() -> None:
    data: Json = {"a": "", "b": 3, "c": "yes", "d": "later"}
    assert payload.first_str(data, "a", "b", "c", "d") == "yes"
    assert payload.first_str(data, "a", "b", "missing") is None
    assert payload.first_str(data) is None


def test_parse_stdin_fixtures() -> None:
    main = stdin_fixture("claude-pretooluse-bash-main.json")
    assert payload.parse_stdin(main) == ("session-raw", None, "high")
    codex = stdin_fixture("codex-sessionstart.json")
    assert payload.parse_stdin(codex) == (
        "01a19ebf-adec-7210-8ba8-550921c2b924",
        "gpt-5.6-sol",
        "high",
    )
    agy = stdin_fixture("agy-posttooluse-write.json")
    assert payload.parse_stdin(agy) == ("conv-raw", "gemini-3.8-flash-low", None)
    assert payload.parse_stdin({"sessionId": "s", "to_model": "m"}) == ("s", "m", None)
    assert payload.parse_stdin({"conversation_id": "c"}) == ("c", None, None)
    assert payload.parse_stdin({"effort": {"x": 1}}) == (None, None, None)
    assert payload.parse_stdin({"effort": {"level": "low"}})[2] == "low"
    assert payload.parse_stdin({}) == (None, None, None)


def test_read_tail_and_parse_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert payload.read_tail(path, 1000) == ["one", "two", "three"]
    assert payload.read_tail(tmp_path / "missing", 10) is None
    path.write_bytes(b"\xff\xfe ok\n")
    assert payload.read_tail(path, 100) == ["\ufffd\ufffd ok"]
    assert payload.parse_line('{"a": 1}') == {"a": 1}
    assert payload.parse_line("[1]") is None
    assert payload.parse_line("nope") is None
    assert payload.parse_line("") is None


def test_read_tail_keeps_only_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("aaaa\nbbbb\ncccc\n", encoding="utf-8")
    assert payload.read_tail(path, 5) == ["cccc"]
    assert payload.read_tail(path, 6) == ["", "cccc"]
    assert payload.read_tail(path, 7) == ["b", "cccc"]
    assert payload.read_tail(path, 14) == ["aaa", "bbbb", "cccc"]
    assert payload.read_tail(path, 15) == ["aaaa", "bbbb", "cccc"]
    assert payload.read_tail(path, 16) == ["aaaa", "bbbb", "cccc"]


def test_defaults_for_missing_fields() -> None:
    assert payload.rec_ts({}) == 0
    assert payload.rec_ts({"ts": 5}) == 5
    assert claude_commands.rec_hashes({}) == set()
    assert claude_commands.rec_hashes({"cmd": ["a", "b"]}) == {"a", "b"}


def test_rollouts_tolerate_invalid_utf8(tmp_path: Path) -> None:
    good = json.dumps(turn("m", "high"))
    path = tmp_path / "r.jsonl"
    path.write_bytes(b"\xff\xfe broken\n" + good.encode() + b"\n")
    assert payload.read_tail(path, 1000) == ["�� broken", good]
    assert codex_rollout.rollout_turn_context(path) == ("m", "high")
    patch = json.dumps(patch_call("a.txt"))
    path.write_bytes(b"\xff Begin Patch\n" + patch.encode() + b"\n")
    assert patch_paths_of(path) == ["a.txt"]
