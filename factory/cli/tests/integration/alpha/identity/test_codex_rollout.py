# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity codex rollout tests."""

import json
import os
import time
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from integration.alpha.identity.identity_test_support import (
    CHILD_THREAD,
    ROOT_THREAD,
    Json,
    fork_lines,
    loaded,
    meta,
    patch_call,
    patch_paths_of,
    receipts_of,
    turn,
    write_rollout,
)

from tbhbdev.alpha.identity import codex_rollout, codex_scan, edits, storage, values


def test_codex_home(tmp_path: Path) -> None:
    assert codex_rollout.codex_home({"CODEX_HOME": "/x/y"}) == Path("/x/y")
    assert codex_rollout.codex_home({}) == tmp_path / "home" / ".codex"
    assert codex_rollout.codex_home({"CODEX_HOME": ""}) == tmp_path / "home" / ".codex"


def test_codex_rollout_lookup(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {"CODEX_HOME": str(home)}
    assert codex_rollout.codex_rollout(CHILD_THREAD, env) is None
    first = write_rollout(home, CHILD_THREAD, [turn("gpt-5.6-sol", "low")])
    assert codex_rollout.codex_rollout(CHILD_THREAD, env) == first
    later = home / "sessions" / "2026" / "11" / "03"
    later.mkdir(parents=True)
    newer = later / f"rollout-2026-11-03T01-00-00-{CHILD_THREAD}.jsonl"
    newer.write_text("{}\n", encoding="utf-8")
    os.utime(first, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))
    assert codex_rollout.codex_rollout(CHILD_THREAD, env) == newer
    os.utime(first, (3_000, 3_000))
    assert codex_rollout.codex_rollout(CHILD_THREAD, env) == first
    for bad in ("../../x", "", "short", "x" * 20, "a/b" + "0" * 10, "0" * 65):
        assert codex_rollout.codex_rollout(bad, env) is None
    assert codex_rollout.codex_rollout("0" * 64, env) is None
    assert codex_rollout.codex_rollout("A" * 8, env) is None


def test_rollout_turn_context_takes_the_latest(tmp_path: Path) -> None:
    path = write_rollout(
        tmp_path,
        CHILD_THREAD,
        [
            meta(source={"subagent": {}}),
            turn("gpt-5.6-sol", "medium"),
            "not json",
            {"type": "other", "note": "turn_context", "payload": {"model": "x"}},
            {"type": "turn_context", "payload": "nope"},
            {"type": "turn_context", "payload": {"model": 3, "effort": ["x"]}},
            turn("gpt-5.6-luna", "low"),
            {"type": "event"},
            {"type": "other", "note": "turn_context"},
        ],
    )
    assert codex_rollout.rollout_turn_context(path) == ("gpt-5.6-luna", "low")
    path = write_rollout(
        tmp_path,
        CHILD_THREAD,
        [turn("a", "b"), {"type": "turn_context", "payload": {}}],
    )
    assert codex_rollout.rollout_turn_context(path) == (None, None)
    path = write_rollout(
        tmp_path, CHILD_THREAD, [turn("a", "b"), "[1]", '["turn_context"]']
    )
    assert codex_rollout.rollout_turn_context(path) == ("a", "b")
    path = write_rollout(
        tmp_path,
        CHILD_THREAD,
        [turn("a", "b"), {"type": "other", "note": "turn_context"}],
    )
    assert codex_rollout.rollout_turn_context(path) == ("a", "b")
    assert codex_rollout.rollout_turn_context(tmp_path / "missing") == (None, None)
    path = write_rollout(tmp_path, CHILD_THREAD, [meta()])
    assert codex_rollout.rollout_turn_context(path) == (None, None)


def test_rollout_turn_context_falls_back_to_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_rollout(tmp_path, CHILD_THREAD, [turn("old", "low"), "x" * 200])
    assert codex_rollout.rollout_turn_context(path) == ("old", "low")
    monkeypatch.setattr(codex_rollout, "CODEX_TAIL", 100)
    assert codex_rollout.rollout_turn_context(path) == ("old", "low")
    assert codex_rollout.CODEX_TAIL == 100
    monkeypatch.undo()
    assert codex_rollout.CODEX_TAIL == 1024 * 1024


@pytest.mark.parametrize("later", [False, True])
def test_rollout_turn_context_long_rollout(tmp_path: Path, *, later: bool) -> None:
    records: list[object] = [turn("first", "high"), "x" * (1024 * 1024 + 100)]
    if later:
        records.append(turn("latest", "low"))
    path = write_rollout(tmp_path, CHILD_THREAD, records)
    assert codex_rollout.rollout_turn_context(path) == (
        ("latest", "low") if later else ("first", "high")
    )


def test_rollout_turn_context_forward_scan_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert codex_rollout.CODEX_HEAD == 1024 * 1024
    monkeypatch.setattr(codex_rollout, "CODEX_TAIL", 1)
    monkeypatch.setattr(codex_rollout, "CODEX_HEAD", 100)
    path = write_rollout(
        tmp_path, CHILD_THREAD, ["x" * 80, "x" * 80, turn("late", "low")]
    )
    assert codex_rollout.rollout_turn_context(path) == (None, None)
    path = write_rollout(tmp_path, CHILD_THREAD, ["x" * 99, turn("late", "low")])
    assert codex_rollout.rollout_turn_context(path) == (None, None)
    path = write_rollout(
        tmp_path,
        CHILD_THREAD,
        [
            "not json",
            {"type": "other", "payload": {"model": "wrong"}},
            {"type": "turn_context", "payload": "invalid"},
            turn("first", "high"),
            turn("second", "low"),
        ],
    )
    monkeypatch.setattr(codex_rollout, "CODEX_HEAD", 1024)
    assert codex_rollout.rollout_turn_context(path) == ("first", "high")
    path.write_bytes(b"\xff\n" + json.dumps(turn("first", "high")).encode() + b"\n")
    assert codex_rollout.rollout_turn_context(path) == ("first", "high")
    path = write_rollout(
        tmp_path, CHILD_THREAD, [{"type": "turn_context", "payload": {}}]
    )
    assert codex_rollout.rollout_turn_context(path) == (None, None)


def test_rollout_meta(tmp_path: Path) -> None:
    path = write_rollout(
        tmp_path, CHILD_THREAD, [meta(id="i", cwd="/c"), turn("m", "e")]
    )
    assert codex_rollout.rollout_meta(path) == {"id": "i", "cwd": "/c"}
    assert codex_rollout.rollout_meta(tmp_path / "missing") == {}
    for first in (
        turn("m", "e"),
        "not json",
        "[1]",
        {"type": "session_meta", "payload": 3},
        {"type": "session_meta"},
    ):
        assert (
            codex_rollout.rollout_meta(write_rollout(tmp_path, CHILD_THREAD, [first]))
            == {}
        )
    assert codex_rollout.rollout_meta(write_rollout(tmp_path, CHILD_THREAD, [])) == {}


RAW_PATCH = (
    "*** Begin Patch\n*** Add File: a.txt\n+x\n*** Update File: dir/b.py\n@@\n-1\n+2\n"
    "*** Delete File: c\n*** Update File: d\n*** Move to: e\n*** End Patch"
)


def test_patch_paths_formats() -> None:
    expected = ["a.txt", "dir/b.py", "c", "d", "e"]
    assert codex_rollout.patch_paths(RAW_PATCH) == expected
    js = (
        'const patch = "*** Begin Patch\\n*** Add File: codex.txt\\n+hi\\n'
        '*** End Patch";\nawait tools.apply_patch(patch);'
    )
    assert codex_rollout.patch_paths(js) == ["codex.txt"]
    assert codex_rollout.patch_paths(json.dumps({"input": RAW_PATCH})) == expected
    assert codex_rollout.patch_paths("no patch here *** Add File: x") == []
    assert codex_rollout.patch_paths(None) == []
    assert codex_rollout.patch_paths(5) == []
    assert codex_rollout.patch_paths(
        "*** Begin Patch\n*** Add File: x\n*** Add File: x\n"
    ) == ["x"]
    assert codex_rollout.patch_paths("*** Begin Patch\n*** Add File:  spaced  \n") == [
        "spaced"
    ]
    assert codex_rollout.patch_paths("*** Begin Patch\r\n*** Add File: w\r\n") == ["w"]
    assert codex_rollout.patch_paths("*** Begin Patch\n*** Add File: tick`\n") == [
        "tick"
    ]
    assert codex_rollout.patch_paths("*** Begin Patch\n*** Add File: at-end") == [
        "at-end"
    ]
    assert codex_rollout.patch_paths("*** Begin Patch\n*** Move to: m'q") == ["m"]


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
    assert codex_rollout.patch_paths(body + "*** End Patch") == paths
    assert codex_rollout.patch_paths(json.dumps({"input": body})) == paths


def test_tool_call_paths() -> None:
    def call(**payload: object) -> Json:
        return {"type": "response_item", "payload": payload}

    assert codex_rollout.tool_call_paths(patch_call("a.txt")) == ["a.txt"]
    assert (
        codex_rollout.tool_call_paths(call(type="custom_tool_call", input=RAW_PATCH))[0]
        == "a.txt"
    )
    assert (
        codex_rollout.tool_call_paths(call(type="function_call", arguments=RAW_PATCH))[
            0
        ]
        == "a.txt"
    )
    assert (
        codex_rollout.tool_call_paths(
            call(type="function_call", input=3, arguments=RAW_PATCH)
        )[0]
        == "a.txt"
    )
    action = {"command": ["apply_patch", RAW_PATCH]}
    assert (
        codex_rollout.tool_call_paths(call(type="local_shell_call", action=action))[0]
        == "a.txt"
    )
    assert codex_rollout.tool_call_paths(call(type="local_shell_call")) == []
    assert (
        codex_rollout.tool_call_paths(call(type="function_call", input=3, arguments=4))
        == []
    )
    assert codex_rollout.tool_call_paths(call(type="message", input=RAW_PATCH)) == []
    assert codex_rollout.tool_call_paths(call(input=RAW_PATCH)) == []
    assert (
        codex_rollout.tool_call_paths({"type": "response_item", "payload": "x"}) == []
    )
    assert codex_rollout.tool_call_paths({"type": "response_item"}) == []
    assert (
        codex_rollout.tool_call_paths(
            {"type": "event", "payload": call(type="function_call")["payload"]}
        )
        == []
    )
    other: Json = {
        "type": "event_msg",
        "payload": {"type": "custom_tool_call", "input": RAW_PATCH},
    }
    assert codex_rollout.tool_call_paths(other) == []


def test_rollout_patch_edits_is_incremental(tmp_path: Path) -> None:
    msg = {
        "type": "response_item",
        "payload": {"type": "message", "content": RAW_PATCH},
    }
    path = write_rollout(
        tmp_path, CHILD_THREAD, [meta(), patch_call("a.txt"), msg, "Begin Patch junk"]
    )
    edits, offset = codex_rollout.rollout_patch_edits(path)
    assert edits == ["a.txt"]
    assert offset == path.stat().st_size
    assert codex_rollout.rollout_patch_edits(path, offset) == ([], offset)
    partial = json.dumps(patch_call("c.txt"))[:30]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(patch_call("b.txt")) + "\n" + partial)
    edits2, offset2 = codex_rollout.rollout_patch_edits(path, offset)
    assert edits2 == ["b.txt"]
    assert offset2 < path.stat().st_size
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert codex_rollout.rollout_patch_edits(path, offset2) == ([], path.stat().st_size)
    assert codex_rollout.rollout_patch_edits(tmp_path / "missing", 7) == ([], 7)
    assert codex_rollout.rollout_patch_edits(path, 10**6) == ([], 10**6)


def test_rollout_patch_edits_counts_every_path_in_order(tmp_path: Path) -> None:
    path = write_rollout(
        tmp_path, CHILD_THREAD, [patch_call("a", "b"), patch_call("c")]
    )
    assert patch_paths_of(path) == ["a", "b", "c"]


def test_codex_threads_select_the_root_sessions_rollouts(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {"CODEX_HOME": str(home)}
    root = write_rollout(
        home, ROOT_THREAD, [meta(id=ROOT_THREAD, session_id=ROOT_THREAD)]
    )
    child = write_rollout(
        home, CHILD_THREAD, [meta(id=CHILD_THREAD, session_id=ROOT_THREAD)]
    )
    write_rollout(home, "0" * 16, [meta(id="other", session_id="other")])
    stale = write_rollout(home, "1" * 16, [meta(id="old", session_id=ROOT_THREAD)])
    os.utime(stale, (1_000, 1_000))
    (home / "sessions" / "2026" / "11" / "02" / "rollout-broken.jsonl").symlink_to(
        tmp_path / "nowhere"
    )
    by_id = write_rollout(home, "2" * 16, [meta(id=ROOT_THREAD)])
    got = codex_rollout.codex_threads(ROOT_THREAD, env)
    assert sorted(p.name for p, _ in got) == sorted(
        p.name for p in (root, child, by_id)
    )
    assert all(isinstance(m, dict) for _, m in got)
    fresh = write_rollout(home, "3" * 16, [meta(id="edge", session_id=ROOT_THREAD)])
    os.utime(fresh, (time.time() - codex_rollout.CODEX_MAX_AGE + 60,) * 2)
    assert fresh in [p for p, _ in codex_rollout.codex_threads(ROOT_THREAD, env)]
    os.utime(fresh, (time.time() - codex_rollout.CODEX_MAX_AGE - 60,) * 2)
    assert fresh not in [p for p, _ in codex_rollout.codex_threads(ROOT_THREAD, env)]
    assert codex_rollout.codex_threads("unknown", env) == []


def test_codex_ident_and_scan(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {"CODEX_HOME": str(home), "CODEX_VERSION": "0.99.0"}
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, session_id=ROOT_THREAD, cwd=str(repo), source="exec"),
            turn("gpt-5.6-sol", "high"),
            patch_call("a.txt", "a.txt"),
        ],
    )
    write_rollout(
        home,
        CHILD_THREAD,
        [
            meta(
                id=CHILD_THREAD,
                session_id=ROOT_THREAD,
                cwd=str(repo),
                source={"subagent": 1},
            ),
            turn("gpt-5.6-luna", "low"),
            patch_call("b.txt"),
        ],
    )
    store = storage.repo_info(repo)
    assert store is not None
    codex_scan.codex_scan(ROOT_THREAD, store, env)
    got = {str(r["path"]): r for r in receipts_of(repo, values.sha256_64(ROOT_THREAD))}
    assert sorted(got) == ["a.txt", "b.txt"]
    assert got["a.txt"]["subagent"] is None
    assert got["a.txt"]["model"] == "openai:gpt-5.6-sol"
    assert got["a.txt"]["harness"] == "codex:0.99.0"
    assert got["a.txt"]["surface"] == "exec"
    assert got["b.txt"]["subagent"] == values.sha256_64(CHILD_THREAD)
    assert got["b.txt"]["model"] == "openai:gpt-5.6-luna"
    assert got["b.txt"]["surface"] is None
    marks = sorted((store.state / "marks").glob("*.json"))
    assert len(marks) == 2
    assert all(isinstance(loaded(m)["offset"], int) for m in marks)
    # A second scan has nothing new; consumed receipts stay consumed.
    for path, rec in edits.load_edits(store, values.sha256_64(ROOT_THREAD)):
        storage.write_json(path, {**rec, "consumed": True})
    codex_scan.codex_scan(ROOT_THREAD, store, env)
    assert receipts_of(repo, values.sha256_64(ROOT_THREAD)) == []


def test_codex_ident_defaults(tmp_path: Path) -> None:
    env = {"CODEX_HOME": str(tmp_path)}
    got = codex_rollout.codex_ident(ROOT_THREAD, {}, None, env)
    assert got == values.Ident(values.sha256_64(ROOT_THREAD), "codex")
    meta_: Json = {"id": CHILD_THREAD, "cli_version": "1.2.3", "source": "cli"}
    got = codex_rollout.codex_ident(ROOT_THREAD, meta_, None, env)
    assert got.subagent == values.sha256_64(CHILD_THREAD)
    assert (got.harness, got.surface) == ("codex:1.2.3", "cli")
    got = codex_rollout.codex_ident(
        ROOT_THREAD, {"id": ROOT_THREAD}, None, {"CODEX_VERSION": "7"}
    )
    assert (got.subagent, got.harness) == (None, "codex:7")


def test_rollout_turn_context_ignores_other_records_with_payloads(
    tmp_path: Path,
) -> None:
    other = {"type": "event_msg", "payload": {"model": "x", "effort": "low"}}
    path = write_rollout(tmp_path, ROOT_THREAD, [turn("m", "high"), other])
    assert codex_rollout.rollout_turn_context(path) == ("m", "high")


def test_tool_call_paths_reads_the_action_of_shell_calls() -> None:
    patch = "*** Begin Patch\n*** Add File: z.txt\n+x\n*** End Patch"
    call: Json = {
        "type": "response_item",
        "payload": {"type": "local_shell_call", "action": {"command": [patch]}},
    }
    assert codex_rollout.tool_call_paths(call) == ["z.txt"]
    payload = cast("Json", call["payload"])
    assert (
        codex_rollout.tool_call_paths({**call, "payload": {**payload, "action": None}})
        == []
    )
    assert (
        codex_rollout.tool_call_paths({**call, "payload": {"type": "local_shell_call"}})
        == []
    )


def test_codex_threads_age_limit_is_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_rollout(tmp_path, ROOT_THREAD, [meta(id=ROOT_THREAD)])
    os.utime(path, (1000, 1000))
    env = {"CODEX_HOME": str(tmp_path)}
    monkeypatch.setattr(time, "time", lambda: 1000 + codex_rollout.CODEX_MAX_AGE)
    assert len(codex_rollout.codex_threads(ROOT_THREAD, env)) == 1
    monkeypatch.setattr(time, "time", lambda: 1001 + codex_rollout.CODEX_MAX_AGE)
    assert codex_rollout.codex_threads(ROOT_THREAD, env) == []


def test_rollout_patch_edits_skips_the_replayed_prefix(tmp_path: Path) -> None:
    path = write_rollout(tmp_path, CHILD_THREAD, fork_lines(4))
    assert patch_paths_of(path) == ["parent.md", "child.md"]
    edits, new = codex_rollout.rollout_patch_edits(path, 0, 4)
    assert edits == ["child.md"]
    assert new == path.stat().st_size
    # A saved offset beyond the prefix wins over the prefix.
    assert codex_rollout.rollout_patch_edits(path, new, 4) == ([], new)
    # A prefix beyond the saved offset wins over the offset.
    assert patch_paths_of(path, 1, 4) == ["child.md"]


def test_rollout_patch_edits_defaults_read_from_the_first_line(
    tmp_path: Path,
) -> None:
    path = write_rollout(tmp_path, CHILD_THREAD, [patch_call("first.txt")])
    assert codex_rollout.rollout_patch_edits(path) == (
        ["first.txt"],
        path.stat().st_size,
    )
