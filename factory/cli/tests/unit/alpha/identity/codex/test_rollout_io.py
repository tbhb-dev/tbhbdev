# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity codex rollout."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import MAIN, patch_call
from unit.alpha.identity.identity_fixtures import (
    CHILD_THREAD,
    ROOT_THREAD,
    child_meta,
    committed_as,
    hook_data,
    hook_rollouts,
    init_repo,
    loaded,
    meta,
    patch_paths_of,
    receipts_of,
    run_hook,
    turn,
    write_rollout,
)

from tbhbdev.alpha.identity import models, receipts, records, storage
from tbhbdev.alpha.identity.codex import rollout as codex_rollout


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
            child_meta(repo),
            turn("gpt-5.6-luna", "low"),
            patch_call("b.txt"),
        ],
    )
    store = storage.repo_info(repo)
    assert store is not None
    codex_rollout.codex_scan(ROOT_THREAD, store, env)
    got = {str(r["path"]): r for r in receipts_of(repo, records.sha256_64(ROOT_THREAD))}
    assert sorted(got) == ["a.txt", "b.txt"]
    assert got["a.txt"]["subagent"] is None
    assert got["a.txt"]["model"] == "openai:gpt-5.6-sol"
    assert got["a.txt"]["harness"] == "codex:0.99.0"
    assert got["a.txt"]["surface"] == "exec"
    assert got["b.txt"]["subagent"] == records.sha256_64(CHILD_THREAD)
    assert got["b.txt"]["model"] == "openai:gpt-5.6-luna"
    assert got["b.txt"]["surface"] is None
    marks = sorted((store.state / "marks").glob("*.json"))
    assert len(marks) == 2
    assert all(isinstance(loaded(m)["offset"], int) for m in marks)
    # A second scan has nothing new; consumed receipts stay consumed.
    for path, rec in receipts.load_edits(store, records.sha256_64(ROOT_THREAD)):
        storage.write_json(path, {**rec, "consumed": True})
    codex_rollout.codex_scan(ROOT_THREAD, store, env)
    assert receipts_of(repo, records.sha256_64(ROOT_THREAD)) == []


def test_codex_scan_falls_back_to_repo_top_without_cwd(
    repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "codex"
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    write_rollout(home, ROOT_THREAD, [meta(id=ROOT_THREAD), patch_call("a.txt")])
    store = storage.repo_info(repo)
    assert store is not None
    codex_rollout.codex_scan(ROOT_THREAD, store, {"CODEX_HOME": str(home)})
    (got,) = receipts_of(repo, records.sha256_64(ROOT_THREAD))
    assert got["path"] == "a.txt"
    assert got["blob"] is not None


def test_rollout_turn_context_ignores_other_records_with_payloads(
    tmp_path: Path,
) -> None:
    other = {"type": "event_msg", "payload": {"model": "x", "effort": "low"}}
    path = write_rollout(tmp_path, ROOT_THREAD, [turn("m", "high"), other])
    assert codex_rollout.rollout_turn_context(path) == ("m", "high")


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


def test_codex_scan_starts_at_zero_and_uses_the_rollout_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = init_repo(tmp_path / "other")
    (other / "o.txt").write_text("o\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home, ROOT_THREAD, [meta(id=ROOT_THREAD, cwd=str(other)), patch_call("o.txt")]
    )
    offsets: list[int] = []
    real = codex_rollout.rollout_patch_edits

    def spy(path: Path, offset: int, skip_lines: int) -> tuple[list[str], int]:
        offsets.append(offset)
        return real(path, offset, skip_lines)

    monkeypatch.setattr(codex_rollout, "rollout_patch_edits", spy)
    info = storage.repo_info()
    assert info is not None
    codex_rollout.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert offsets == [0]
    root = records.sha256_64(ROOT_THREAD)
    assert [r["path"] for r in receipts_of(other, root)] == ["o.txt"]
    assert receipts_of(repo, root) == []


def fork_lines(replayed: int) -> list[object]:
    """A synthetic forked rollout: ``replayed`` inherited lines, then its own."""
    inherited: list[object] = [
        meta(
            id=CHILD_THREAD,
            session_id=ROOT_THREAD,
            forked_from_id=ROOT_THREAD,
            **{START: replayed},
        ),
        patch_call("parent.md"),
        *[{"type": "response_item", "payload": {"type": "message"}}] * (replayed - 2),
    ]
    own: list[object] = [
        {"type": "event_msg", "payload": {"type": "thread_settings_applied"}},
        patch_call("child.md"),
    ]
    return [*inherited, *own]


START = "subagent_history_start_ordinal"


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


def test_codex_scan_ignores_patches_replayed_into_a_fork(
    repo: Path, tmp_path: Path
) -> None:
    for name in ("parent.md", "child.md"):
        (repo / name).write_text("x\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, session_id=ROOT_THREAD), patch_call("parent.md")],
    )
    write_rollout(home, CHILD_THREAD, fork_lines(4))
    info = storage.repo_info()
    assert info is not None
    codex_rollout.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    by_thread = {
        r["path"]: r["subagent"]
        for r in receipts_of(repo, records.sha256_64(ROOT_THREAD))
    }
    assert by_thread == {"parent.md": None, "child.md": records.sha256_64(CHILD_THREAD)}


@pytest.mark.parametrize("bad", [None, 0, -3, "4", 2.5])
def test_codex_scan_tolerates_odd_fork_markers(
    repo: Path, tmp_path: Path, bad: object
) -> None:
    (repo / "parent.md").write_text("x\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home,
        CHILD_THREAD,
        [
            meta(id=CHILD_THREAD, session_id=ROOT_THREAD, **{START: bad}),
            patch_call("parent.md"),
        ],
    )
    info = storage.repo_info()
    assert info is not None
    codex_rollout.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert len(receipts_of(repo, records.sha256_64(ROOT_THREAD))) == 1


@pytest.mark.usefixtures("repo")
def test_refresh_codex_scans_only_for_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, storage.Repo, object]] = []

    def spy(root: str, info: storage.Repo, env: object) -> None:
        calls.append((root, info, env))

    monkeypatch.setattr(codex_rollout, "codex_scan", spy)
    info = storage.repo_info()
    assert info is not None
    env = {"CODEX_SESSION_ID": ROOT_THREAD}
    codex_rollout.refresh_codex(models.Committer(models.CODEX, MAIN, MAIN), info, env)
    assert calls == [(ROOT_THREAD, info, env)]
    codex_rollout.refresh_codex(committed_as(MAIN), info, env)
    codex_rollout.refresh_codex(
        models.Committer(models.ANTIGRAVITY, MAIN, MAIN), info, env
    )
    assert len(calls) == 1


def test_codex_scan_credits_a_forks_replayed_patches_to_the_parent_only(
    repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, session_id=ROOT_THREAD), patch_call("parent.md")],
    )
    write_rollout(home, CHILD_THREAD, fork_lines(4))
    info = storage.repo_info()
    assert info is not None
    codex_rollout.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    got = sorted(
        (str(r["path"]), str(r["subagent"]))
        for r in receipts_of(repo, records.sha256_64(ROOT_THREAD))
    )
    assert got == [
        ("child.md", records.sha256_64(CHILD_THREAD)),
        ("parent.md", "None"),
    ]


@pytest.mark.parametrize("hooked", ["root", "child"])
def test_codex_scan_takes_nothing_from_a_hooked_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hooked: str
) -> None:
    home = tmp_path / "codex"
    root, child = hook_rollouts(home, repo)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    (repo / "sub.txt").write_text("sub\n", encoding="utf-8")
    # Only one thread has a hook record; the other never fired a hook.
    start, rollout = (
        ("codex-hook-sessionstart.json", root)
        if hooked == "root"
        else ("codex-hook-subagentstart.json", child)
    )
    run_hook(monkeypatch, hook_data(start, repo, rollout))
    for path, name in ((root, "root.txt"), (child, "sub.txt")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(patch_call(name)) + "\n")
    store = storage.repo_info(repo)
    assert store is not None
    checked: list[str] = []
    real = Path.exists

    def spy(self: Path) -> bool:
        checked.append(self.parent.name)
        return real(self)

    monkeypatch.setattr(Path, "exists", spy)
    # The scan finds the hook records from the store, wherever the process is.
    monkeypatch.chdir(tmp_path)
    codex_rollout.codex_scan(ROOT_THREAD, store, {"CODEX_HOME": str(home)})
    monkeypatch.undo()
    assert "sessions" in checked
    got = receipts_of(repo, records.sha256_64(ROOT_THREAD))
    # The hooked thread's rollout patch added nothing; the other's was read.
    expect = ("sub.txt", False) if hooked == "root" else ("root.txt", True)
    assert [(str(r["path"]), r["subagent"] is None) for r in got] == [expect]


def test_rollout_patch_edits_defaults_read_from_the_first_line(
    tmp_path: Path,
) -> None:
    path = write_rollout(tmp_path, CHILD_THREAD, [patch_call("first.txt")])
    assert codex_rollout.rollout_patch_edits(path) == (
        ["first.txt"],
        path.stat().st_size,
    )
