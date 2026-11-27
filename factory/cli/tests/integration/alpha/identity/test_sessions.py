# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity sessions tests."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from integration.alpha.identity.identity_test_support import (
    AGENT,
    CHILD_THREAD,
    CLAUDE_COMMIT_ENV,
    EARLIER,
    EARLIER_TS,
    EARLY,
    LATE,
    LAUNCH,
    NOW_UTC,
    OPUS,
    OTHER,
    ROOT,
    ROOT_THREAD,
    SESSION_HASH,
    SONNET,
    SRC,
    add_worktree,
    claude_event,
    hook_data,
    hook_env,
    hook_rollouts,
    init_repo,
    loaded,
    make_ident,
    meta,
    no_start,
    patch_call,
    reply,
    run_hook,
    seed_claude,
    session_file,
    write_rollout,
    write_transcript,
)

from tbhbdev.alpha.identity import (
    attribution,
    claude_commands,
    codex_scan,
    edits,
    sessions,
    storage,
    values,
)

RECORD_WRITER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tbhbdev.alpha.identity import sessions
for i in range(20):
    sessions.write_record(sys.argv[3], {f"{sys.argv[4]}-{i}": "v"}, Path(sys.argv[2]))
"""


def test_concurrent_record_writers_lose_no_update(repo: Path) -> None:
    """Regression: unlocked read-modify-write lost most of these updates."""
    writers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                RECORD_WRITER,
                str(SRC),
                str(repo),
                ROOT,
                str(n),
            ],
            env=hook_env(),
        )
        for n in range(10)
    ]
    assert [w.wait() for w in writers] == [0] * 10
    assert len(sessions.load_record(ROOT)) == 200


def test_a_session_from_the_main_checkout_is_known_in_a_worktree(
    repo: Path, tmp_path: Path
) -> None:
    sessions.write_record(ROOT, {"model": "m"}, repo)
    tree = add_worktree(repo, tmp_path / "wt")
    assert sessions.load_record(ROOT, tree) == {"model": "m"}
    sessions.write_record(ROOT, {"model": "n"}, tree)
    assert sessions.load_record(ROOT, tree) == {"model": "n"}
    assert sessions.load_record(ROOT, repo) == {"model": "m"}


def test_write_record_merges_without_clobbering(repo: Path) -> None:
    sessions.write_record(ROOT, {"model": "m", "effort": None})
    sessions.write_record(ROOT, {"effort": "low", "model": ""})
    assert sessions.load_record(ROOT) == {"model": "m", "effort": "low"}
    path = repo / ".git" / "tbhb-alpha" / "sessions" / f"{'a' * 16}.json"
    assert loaded(path) == {"model": "m", "effort": "low"}


def test_records_honor_the_cwd_argument(repo: Path, plain: Path) -> None:
    # `plain` is the working directory; `repo` is only reachable by argument.
    sessions.write_record(ROOT, {"model": "m"}, repo)
    assert sessions.load_record(ROOT, repo) == {"model": "m"}
    assert loaded(repo / ".git" / "tbhb-alpha" / "sessions" / f"{'a' * 16}.json")
    assert list(plain.iterdir()) == []


def test_a_session_is_known_in_another_clone(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.write_record(ROOT, {"model": "m", "effort": "low"}, repo)
    other = init_repo(tmp_path / "other")
    monkeypatch.chdir(other)
    assert sessions.load_record(ROOT) == {"model": "m", "effort": "low"}
    assert sessions.load_record(ROOT, other) == {"model": "m", "effort": "low"}
    assert not (other / ".git" / "tbhb-alpha").exists()


def test_records_outside_a_repository_go_to_the_user_store(plain: Path) -> None:
    sessions.write_record(ROOT, {"model": "m"})
    assert sessions.load_record(ROOT) == {"model": "m"}
    assert list(plain.iterdir()) == []
    assert (storage.user_store() / f"{values.hx(ROOT)}.json").is_file()


def test_store_directory_names(repo: Path, tmp_path: Path) -> None:
    store = repo / ".git" / "tbhb-alpha"
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    sessions.write_record(ROOT, {"model": "m"})
    claude_commands.record_command(ROOT, None, "ls")
    edits.write_edit(make_ident(), str(repo / "a.txt"))
    home = tmp_path / "codex"
    write_rollout(home, ROOT_THREAD, [meta(id=ROOT_THREAD), patch_call("a.txt")])
    info = storage.repo_info()
    assert info is not None
    codex_scan.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert sorted(os.listdir(store)) == [  # noqa: PTH208 -- exact on-disk names
        "cmds",
        "edits",
        "lock",
        "marks",
        "marks.lock",
        "sessions",
    ]
    feed_sid = values.sha256_64("fresh")
    storage.write_json(store / "sessions" / f"{values.hx(feed_sid)}.json", {})
    assert sorted(os.listdir(store / "sessions"))  # noqa: PTH208 -- exact on-disk names


def test_codex_sessionstart_records_the_root_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    run_hook(monkeypatch, hook_data("codex-hook-sessionstart.json", repo, root))
    assert no_start(sessions.load_record(values.sha256_64(ROOT_THREAD))) == {
        "harness": "codex:0.166.0",
        "root": values.sha256_64(ROOT_THREAD),
        "model": "openai:gpt-5.6-sol",
        "effort": "high",
        "surface": "exec",
        "transcript": str(root),
    }


def test_codex_subagentstart_records_the_executing_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, child = hook_rollouts(tmp_path / "codex", repo)
    run_hook(monkeypatch, hook_data("codex-hook-subagentstart.json", repo, child))
    # Keyed by the subagent; the model is stdin's, effort the rollout's.
    assert no_start(sessions.load_record(values.sha256_64(CHILD_THREAD))) == {
        "harness": "codex:0.166.0",
        "root": values.sha256_64(ROOT_THREAD),
        "agent_type": "default",
        "model": "openai:gpt-5.6-luna",
        "effort": "low",
        "transcript": str(child),
    }
    assert sessions.load_record(values.sha256_64(ROOT_THREAD)) == {}


def test_load_record_takes_the_latest_switch_as_a_pair(
    repo: Path, tmp_path: Path
) -> None:
    tree = add_worktree(repo, tmp_path / "wt")
    user = storage.user_store() / f"{values.hx(SESSION_HASH)}.json"
    storage.write_json(user, {"switch_model": "u", "switch_at": 5.0, "version": "u"})
    storage.write_json(
        session_file(repo / ".git" / "tbhb-alpha"),
        {"switch_model": "main", "switch_at": 9.0, "version": "main"},
    )
    own = session_file(repo / ".git" / "worktrees" / "wt" / "tbhb-alpha")
    storage.write_json(own, {"switch_model": "own", "switch_at": 7.0, "version": "own"})
    # Other fields: the committing worktree wins. The switch: the latest wins.
    assert sessions.load_record(SESSION_HASH, tree) == {
        "switch_model": "main",
        "switch_at": 9.0,
        "version": "own",
    }
    # A switch time that isn't a float is not a switch.
    storage.write_json(own, {"switch_model": "own", "switch_at": 99, "version": "own"})
    got = sessions.load_record(SESSION_HASH, tree)
    assert (got["switch_model"], got["switch_at"]) == ("main", 9.0)
    # The user's store can hold the latest switch too, and a tie keeps the first.
    # "a" sorts before "main", so a tie is never settled by the model name.
    storage.write_json(user, {"switch_model": "a", "switch_at": 9.0})
    got = sessions.load_record(SESSION_HASH, tree)
    assert (got["switch_model"], got["switch_at"]) == ("a", 9.0)
    storage.write_json(user, {"switch_model": "u", "switch_at": 10.0})
    got = sessions.load_record(SESSION_HASH, tree)
    assert (got["switch_model"], got["switch_at"]) == ("u", 10.0)


def test_load_record_without_a_switch_adds_none(repo: Path) -> None:
    sessions.write_record(SESSION_HASH, {"version": "1"}, repo)
    assert sessions.load_record(SESSION_HASH, repo) == {"version": "1"}
    assert sessions.load_record(OTHER, repo) == {}


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [(EARLIER, OPUS), ("not a time", SONNET)],
    ids=["tie-goes-to-the-transcript", "unreadable-time-lets-the-switch-win"],
)
def test_a_commit_compares_the_switch_with_the_transcript_time(
    repo: Path, timestamp: str, expected: str
) -> None:
    transcript = write_transcript(repo / "t.jsonl", reply(OPUS, timestamp))
    sessions.write_record(
        SESSION_HASH,
        {
            "transcript": str(transcript),
            "switch_model": SONNET,
            "switch_at": EARLIER_TS,
        },
        repo,
    )
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, [])
    assert got is not None
    assert got.ident.model == "anthropic:" + expected


@pytest.mark.parametrize(
    "kind", ["none", "session", "done", "expired", "future", "main", "foreign"]
)
def test_unmatched_commit_keeps_main_identity_without_overlapping_child_command(
    repo: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    seed_claude(repo)
    monkeypatch.setattr(time, "time", lambda: 10000.0)
    if kind == "session":
        sessions.write_record(AGENT, {"root": SESSION_HASH}, repo)
    elif kind != "none":
        root = OTHER if kind == "foreign" else SESSION_HASH
        agent = None if kind == "main" else AGENT
        claude_commands.record_command(root, agent, "ls", repo)
        if kind == "done":
            claude_commands.finish_command(root, "ls", agent, repo)
        elif kind == "expired":
            monkeypatch.setattr(
                time, "time", lambda: 10000.0 + claude_commands.CMD_WINDOW
            )
        elif kind == "future":
            monkeypatch.setattr(time, "time", lambda: 9999.0)
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, ["unmatched"])
    assert got is not None
    assert got.ident.subagent is None
    assert got.ident.model == "anthropic:claude-opus-5-5"


def test_write_record_sets_once_fields_only_where_absent(repo: Path) -> None:
    sessions.write_record(
        ROOT, {"model": "m"}, once={"started_at": EARLY, "launch": None}
    )
    sessions.write_record(
        ROOT, {"model": "n"}, once={"started_at": LATE, "launch": LAUNCH}
    )
    expected = {"model": "n", "started_at": EARLY, "launch": LAUNCH}
    name = f"{'a' * 16}.json"
    assert loaded(repo / ".git" / "tbhb-alpha" / "sessions" / name) == expected
    assert loaded(storage.user_store() / name) == expected
    sessions.write_user_record(ROOT, {"model": "o"})
    assert loaded(storage.user_store() / name) == {**expected, "model": "o"}


@pytest.mark.usefixtures("repo", "clock")
def test_an_interactive_session_starts_at_its_first_hook_event() -> None:
    claude_event(Path.cwd(), "one")
    rec = sessions.load_record(values.sha256_64("one"))
    assert rec["started_at"] == NOW_UTC
    assert "launch" not in rec
