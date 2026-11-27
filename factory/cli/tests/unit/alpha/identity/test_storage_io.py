# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity storage."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from unit.alpha.identity.identity_examples import CHILD, OTHER, ROOT, patch_call
from unit.alpha.identity.identity_fixtures import (
    DATA,
    add_worktree,
    feed,
    hook_env,
    init_repo,
    loaded,
    patch_paths_of,
    pending,
    turn,
)

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import records, sessions, storage
from tbhbdev.alpha.identity.claude import commands as claude_commands
from tbhbdev.alpha.identity.codex import rollout as codex_rollout
lazy from tbhbdev.alpha.identity.records import Json


def test_read_hash_vectors_from_json() -> None:
    stored = storage.read_json(DATA / "hash-vectors.json")
    assert stored is not None
    vectors = stored["vectors"]
    assert isinstance(vectors, list)
    assert len(vectors) >= 3
    for vector in cast("list[dict[str, str]]", vectors):
        raw = bytes.fromhex(vector["input_hex"]).decode()
        assert raw == vector["input_utf8"]
        assert records.sha256_64(raw) == vector["expected"]


def test_read_json_defaults(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    listy = tmp_path / "list.json"
    listy.write_text("[1]", encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    fallback: Json = {"d": 1}
    assert storage.read_json(good, fallback) == {"a": 1}
    assert storage.read_json(listy, fallback) is fallback
    assert storage.read_json(bad, fallback) is fallback
    assert storage.read_json(tmp_path / "missing.json", fallback) is fallback
    assert storage.read_json(bad) is None
    assert storage.read_record(good) == {"a": 1}
    assert storage.read_record(bad) == {}
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert storage.read_record(empty) == {}


def test_write_json_is_atomic_and_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "r.json"
    storage.write_json(target, {"k": "v"})
    assert loaded(target) == {"k": "v"}
    storage.write_json(target, {"k": "w"})
    assert loaded(target) == {"k": "w"}
    assert [p.name for p in target.parent.iterdir()] == ["r.json"]


@settings(deadline=None)
@given(
    st.dictionaries(
        st.text(max_size=8),
        st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8)),
    )
)
def test_json_round_trip(obj: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.json"
        storage.write_json(path, obj)
        assert storage.read_json(path) == obj


LOCK_PROBE = """
import fcntl, sys
with open(sys.argv[1], "a") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
"""


def lock_is_free(path: Path) -> bool:
    done = subprocess.run(
        [sys.executable, "-c", LOCK_PROBE, str(path)], check=False, env=hook_env()
    )
    return done.returncode == 0


def test_locked_holds_an_exclusive_lock_and_creates_the_store(
    tmp_path: Path,
) -> None:
    store = tmp_path / "x" / "tbhb-alpha"
    with storage.locked(store):
        assert not lock_is_free(store / "lock")
    assert lock_is_free(store / "lock")
    with storage.locked(store):  # the store exists now
        pass
    assert [p.name for p in store.iterdir()] == ["lock"]


def test_git_reports_exit_code_and_trims_newlines(
    repo: Path, plain_checkout: Path
) -> None:
    assert git_repo.git("rev-parse", "--show-toplevel", cwd=repo) == (0, str(repo))
    code, out = git_repo.git("rev-parse", "--git-dir", cwd=plain_checkout)
    assert code != 0
    assert out == ""
    code, out = git_repo.git("--version")
    assert code == 0
    assert out.startswith("git version")


def test_state_dir_and_repo_info(
    repo: Path, plain_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    store = repo / ".git" / "tbhb-alpha"
    assert storage.state_dir() == store
    assert storage.state_dir(repo) == store
    assert storage.state_dir(plain_checkout) is None
    assert storage.stores(plain_checkout) == []
    assert storage.repo_info() == storage.Repo(repo, store, store)
    assert storage.repo_info(repo) == storage.Repo(repo, store, store)
    assert storage.repo_info(plain_checkout) is None


def test_stores_in_a_path_with_a_space(tmp_path: Path) -> None:
    spaced = init_repo(tmp_path / "a b")
    store = spaced / ".git" / "tbhb-alpha"
    assert storage.stores(spaced) == [store]


def test_each_worktree_has_its_own_store(repo: Path, tmp_path: Path) -> None:
    tree = add_worktree(repo, tmp_path / "wt")
    common = repo / ".git"
    own = common / "worktrees" / "wt" / "tbhb-alpha"
    assert storage.repo_info(tree) == storage.Repo(tree, own, common / "tbhb-alpha")
    assert storage.state_dir(tree) == own
    assert storage.stores(tree) == [common / "tbhb-alpha", own]
    assert storage.stores(repo) == [own, common / "tbhb-alpha"]


def test_the_repository_record_outranks_the_user_store(repo: Path) -> None:
    storage.user_store().mkdir(parents=True)
    storage.write_private_json(
        storage.user_store() / f"{'a' * 16}.json", {"model": "u", "effort": "high"}
    )
    storage.write_json(
        repo / ".git" / "tbhb-alpha" / "sessions" / f"{'a' * 16}.json", {"model": "m"}
    )
    assert sessions.load_record(ROOT) == {"model": "m", "effort": "high"}


def test_the_user_store_is_private(repo: Path, tmp_path: Path) -> None:
    user = storage.user_store()
    assert user == tmp_path / "home" / ".local" / "state" / "tbhbdev" / "tbhb-alpha"
    user.mkdir(parents=True, mode=0o755)
    user.chmod(0o755)
    leftover = user / f"{'a' * 16}.json.{os.getpid()}.tmp"
    leftover.write_text("")
    leftover.chmod(0o644)
    old = os.umask(0)
    try:
        sessions.write_record(ROOT, {"model": "m"}, repo)
        sessions.write_record(ROOT, {"effort": "low"}, repo)
    finally:
        os.umask(old)
    record = user / f"{'a' * 16}.json"
    assert stat.S_IMODE(user.stat().st_mode) == 0o700
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert loaded(record) == {"model": "m", "effort": "low"}
    assert sorted(p.name for p in user.iterdir()) == ["aaaaaaaaaaaaaaaa.json", "lock"]


def test_a_user_store_it_cant_write_keeps_the_repository_record(repo: Path) -> None:
    storage.user_store().parent.mkdir(parents=True)
    storage.user_store().write_text("not a directory")
    sessions.write_record(ROOT, {"model": "m"}, repo)
    path = repo / ".git" / "tbhb-alpha" / "sessions" / f"{'a' * 16}.json"
    assert loaded(path) == {"model": "m"}
    assert storage.user_store().read_text() == "not a directory"


def test_the_user_store_follows_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert storage.user_store() == tmp_path / "state" / "tbhbdev" / "tbhb-alpha"
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert (
        storage.user_store()
        == Path.home() / ".local" / "state" / "tbhbdev" / "tbhb-alpha"
    )


def test_records_outside_a_repository_go_to_the_user_store(
    plain_checkout: Path,
) -> None:
    sessions.write_record(ROOT, {"model": "m"})
    assert sessions.load_record(ROOT) == {"model": "m"}
    assert list(plain_checkout.iterdir()) == []
    assert (storage.user_store() / f"{records.hx(ROOT)}.json").is_file()


def test_read_tail_decodes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert storage.read_tail(path, 1000) == ["one", "two", "three"]
    assert storage.read_tail(tmp_path / "missing", 10) is None
    path.write_bytes(b"\xff\xfe ok\n")
    assert storage.read_tail(path, 100) == ["\ufffd\ufffd ok"]


def test_read_tail_keeps_only_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("aaaa\nbbbb\ncccc\n", encoding="utf-8")
    assert storage.read_tail(path, 5) == ["cccc"]
    assert storage.read_tail(path, 6) == ["", "cccc"]
    assert storage.read_tail(path, 7) == ["b", "cccc"]
    assert storage.read_tail(path, 14) == ["aaa", "bbbb", "cccc"]
    assert storage.read_tail(path, 15) == ["aaaa", "bbbb", "cccc"]
    assert storage.read_tail(path, 16) == ["aaaa", "bbbb", "cccc"]


def test_the_user_store_keeps_commands_private(repo: Path) -> None:
    old = os.umask(0)
    try:
        claude_commands.record_command(ROOT, CHILD, "ls", repo)
        user = storage.user_store()
        assert "cmds" in {child.name for child in user.iterdir()}
        (path,) = (user / "cmds" / ("a" * 16)).glob("*.json")
        assert stat.S_IMODE(user.stat().st_mode) == 0o700
        references = user / "cmd-stores" / records.hx(ROOT)
        assert stat.S_IMODE(references.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        (rec,) = pending(repo)
        assert loaded(path) == rec
        assert (
            path.name
            == next((repo / ".git" / "tbhb-alpha" / "cmds").rglob("*.json")).name
        )
        claude_commands.finish_command(ROOT, "ls", CHILD, repo)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert loaded(path)["done"] is not None


def test_a_user_store_it_cant_write_keeps_the_command_records(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.user_store().parent.mkdir(parents=True)
    storage.user_store().write_text("not a directory")
    claude_commands.record_command(ROOT, CHILD, "ls", repo)
    monkeypatch.chdir(tmp_path)
    assert claude_commands.match_command_agent(ROOT, ["sh -c eval 'ls'"], repo) == CHILD
    claude_commands.finish_command(ROOT, "ls", CHILD, repo)
    (rec,) = pending(repo)
    assert rec["agent"] == CHILD
    assert rec["done"] is not None
    assert storage.user_store().read_text() == "not a directory"


class FakeRun:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_read_hook_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    feed(monkeypatch, '{"a": 1}')
    assert git_repo.read_hook_stdin() == {"a": 1}
    feed(monkeypatch, "")
    assert git_repo.read_hook_stdin() == {}
    feed(monkeypatch, "[1]")
    assert git_repo.read_hook_stdin() == {}
    feed(monkeypatch, "{")
    assert git_repo.read_hook_stdin() == {}


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("Xa\n", "Xa"), ("  Xa  \n\n", "  Xa  "), (" a \n", " a "), ("\n", "")],
)
def test_git_trims_only_newlines(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake(argv: list[str], **kwargs: object) -> FakeRun:
        calls.append((argv, kwargs))
        return FakeRun(3, stdout)

    monkeypatch.setattr(subprocess, "run", fake)
    assert git_repo.git("status", "-s", cwd=Path("/w")) == (3, expected)
    assert calls == [
        (
            ["git", "status", "-s"],
            {"capture_output": True, "text": True, "cwd": Path("/w"), "check": False},
        )
    ]


def test_repo_info_handles_spaces_in_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = init_repo(tmp_path / "my repo")
    monkeypatch.chdir(path)
    store = path / ".git" / "tbhb-alpha"
    assert storage.repo_info() == storage.Repo(path, store, store)


def test_rollouts_tolerate_invalid_utf8(tmp_path: Path) -> None:
    good = json.dumps(turn("m", "high"))
    path = tmp_path / "r.jsonl"
    path.write_bytes(b"\xff\xfe broken\n" + good.encode() + b"\n")
    assert storage.read_tail(path, 1000) == ["�� broken", good]
    assert codex_rollout.rollout_turn_context(path) == ("m", "high")
    patch = json.dumps(patch_call("a.txt"))
    path.write_bytes(b"\xff Begin Patch\n" + patch.encode() + b"\n")
    assert patch_paths_of(path) == ["a.txt"]


def test_command_store_references_follow_the_root_and_keep_local_precedence(
    repo: Path, tmp_path: Path
) -> None:
    other = init_repo(tmp_path / "other clone")
    claude_commands.record_command(ROOT, CHILD, "ls", other)
    claude_commands.record_command(ROOT, CHILD, "pwd", repo)
    references = storage.user_store() / "cmd-stores" / records.hx(ROOT)
    storage.write_json(references / "invalid.json", {"store": 5})
    storage.write_json(references / "empty.json", {})
    remote = other / ".git" / storage.STORE
    local = repo / ".git" / storage.STORE
    assert sessions.session_stores(ROOT, repo) == [remote, local]
    assert sessions.session_stores(OTHER, repo) == [local]
    assert set(sessions.session_stores(ROOT, tmp_path)) == {remote, local}
    for store in (remote, local):
        reference = references / f"{records.hx(records.sha256_64(str(store)))}.json"
        assert loaded(reference) == {"store": str(store)}
        assert stat.S_IMODE(reference.stat().st_mode) == 0o600
    assert stat.S_IMODE(references.stat().st_mode) == 0o700


def test_project_store_search_keeps_current_records_last_and_reads_each_store_once(
    repo: Path, tmp_path: Path
) -> None:
    other = init_repo(tmp_path / "root project")
    claude_commands.record_command(ROOT, CHILD, "ls", other)
    local = repo / ".git" / storage.STORE
    remote = other / ".git" / storage.STORE
    assert sessions.session_stores(ROOT, project=other) == [remote, local]
    assert sessions.session_stores(ROOT, project=repo) == [remote, local]
    assert sessions.session_stores(ROOT, other) == [remote]
    sessions.write_record(ROOT, {"model": "main"}, repo)
    sessions.write_record(ROOT, {"model": "project"}, other)
    assert sessions.load_record(ROOT, project=other)["model"] == "main"
