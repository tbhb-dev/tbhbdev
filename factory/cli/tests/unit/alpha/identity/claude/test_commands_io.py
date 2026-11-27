# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity claude commands."""

from __future__ import annotations

import itertools
import json
import os
import time
from typing import cast
lazy from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import (
    CHILD,
    OTHER,
    ROOT,
    SESSION_HASH,
    H,
    make_ident,
    patch_call,
)
from unit.alpha.identity.identity_fixtures import (
    AGENT,
    CLAUDE_COMMIT_ENV,
    ROOT_THREAD,
    init_repo,
    loaded,
    meta,
    msg_file,
    pending,
    seed_claude,
    stage,
    write_rollout,
)

from tbhbdev.alpha.identity import (
    attribution,
    dispatcher,
    receipts,
    records,
    sessions,
    storage,
)
from tbhbdev.alpha.identity.claude import ancestry as claude_ancestry
from tbhbdev.alpha.identity.claude import commands as claude_commands
from tbhbdev.alpha.identity.claude import matching as claude_matching
from tbhbdev.alpha.identity.codex import rollout as codex_rollout
from tbhbdev.alpha.identity.commands import cmd_hashes
lazy from tbhbdev.alpha.identity.records import Json


def test_command_matching_and_ambiguity(repo: Path) -> None:
    def line(cmd: str) -> str:
        return f"/bin/zsh -c source s && eval '{cmd}' < /dev/null && pwd -P >| /t"

    claude_commands.record_command(ROOT, CHILD, "git commit -m x", repo)
    claude_commands.record_command(ROOT, None, "ls", repo)
    match = claude_commands.match_command_agent
    assert match(ROOT, [line("git commit -m x")], repo) == CHILD
    assert match(ROOT, [line("ls")], repo) is None
    with pytest.raises(records.AttributionError):
        match(ROOT, [line("other")], repo)
    with pytest.raises(records.AttributionError):
        match(ROOT, [], repo)
    # The harness may trim the command it runs.
    assert match(ROOT, [line("git commit -m x  ")], repo) == CHILD
    claude_commands.record_command(ROOT, CHILD, "padded  ", repo)
    assert match(ROOT, [line("padded")], repo) == CHILD
    # Identical commands from two agents are ambiguous.
    claude_commands.record_command(ROOT, OTHER, "git commit -m x", repo)
    with pytest.raises(records.AttributionError):
        match(ROOT, [line("git commit -m x")], repo)
    # The main agent and a subagent running the same command are ambiguous too.
    claude_commands.record_command(ROOT, None, "shared", repo)
    claude_commands.record_command(ROOT, CHILD, "shared", repo)
    with pytest.raises(records.AttributionError):
        match(ROOT, [line("shared")], repo)
    # Finished records stop matching, so sequential repeats are fine.
    claude_commands.finish_command(ROOT, "git commit -m x", OTHER, repo)
    assert match(ROOT, [line("git commit -m x")], repo) == CHILD
    # Other sessions never match.
    assert match(H + "9" * 16, [line("git commit -m x")], repo) is None


def test_match_command_agent_window_and_defaults(
    repo: Path, plain_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = "sh -c eval 'ls'"
    claude_commands.record_command(ROOT, CHILD, "ls", repo)
    assert claude_commands.match_command_agent(ROOT, [line], repo) == CHILD
    far = time_of(repo) + claude_matching.CMD_WINDOW
    assert claude_commands.match_command_agent(ROOT, [line], repo, now=far - 1) == CHILD
    assert claude_commands.match_command_agent(ROOT, [line], repo, now=far + 1) is None
    monkeypatch.setattr(time, "time", lambda: far + 1)
    assert claude_commands.match_command_agent(ROOT, [line], repo) is None
    monkeypatch.chdir(plain_checkout)
    assert claude_commands.match_command_agent(ROOT, [line]) is None


def test_a_multi_line_command_matches_the_ps_line_on_macos_and_linux(
    repo: Path,
) -> None:
    cmd = "cd x &&\n\tgit commit -m 'a b'\n"
    claude_commands.record_command(ROOT, CHILD, cmd, repo)
    quoted = cmd.replace("'", "'\\''")
    macos = quoted.replace("\n", r"\012").replace("\t", r"\011")
    linux = quoted.replace("\n", " ").replace("\t", " ")
    for shown in (macos, linux):
        line = f"/bin/zsh -c source s.sh && eval '{shown}' < /dev/null && pwd -P"
        assert claude_commands.match_command_agent(ROOT, [line], repo) == CHILD


def test_a_commit_in_another_clone_finds_the_agent(repo: Path, tmp_path: Path) -> None:
    line = "sh -c eval 'git commit -m x'"
    other = init_repo(tmp_path / "other")
    claude_commands.record_command(ROOT, CHILD, "git commit -m x", repo)
    assert claude_commands.match_command_agent(ROOT, [line], other) == CHILD
    claude_commands.finish_command(ROOT, "git commit -m x", CHILD, repo)
    assert claude_commands.match_command_agent(ROOT, [line], other) is None
    assert not (other / ".git" / "tbhb-alpha").exists()


def test_a_repository_store_it_cant_lock_still_finishes_the_user_copy(
    repo: Path,
) -> None:
    claude_commands.record_command(ROOT, CHILD, "ls", repo)
    (repo / ".git" / "tbhb-alpha" / "lock").mkdir()
    claude_commands.finish_command(ROOT, "ls", CHILD, repo)
    (rec,) = pending(repo)
    assert rec["done"] is None
    (path,) = (storage.user_store() / "cmds" / ("a" * 16)).glob("*.json")
    assert loaded(path)["done"] is not None


def test_commands_from_a_directory_outside_a_repository_still_match(
    repo: Path, plain_checkout: Path
) -> None:
    # A teammate runs in a session whose directory is no repository, and commits
    # in a clone: its command is found through the user store alone.
    claude_commands.record_command(ROOT, CHILD, "git commit -m x", plain_checkout)
    assert list(plain_checkout.iterdir()) == []
    assert not (storage.user_store() / "cmd-stores").exists()
    match = claude_commands.match_command_agent
    assert match(ROOT, ["sh -c eval 'git commit -m x'"], repo) == CHILD
    claude_commands.finish_command(ROOT, "git commit -m x", CHILD, plain_checkout)
    assert match(ROOT, ["sh -c eval 'git commit -m x'"], repo) is None
    (path,) = (storage.user_store() / "cmds" / ("a" * 16)).glob("*.json")
    assert loaded(path)["done"] is not None


def time_of(repo: Path) -> float:
    (path,) = (repo / ".git" / "tbhb-alpha" / "cmds" / ("a" * 16)).glob("*.json")
    return float(cast("float", loaded(path)["ts"]))


def test_record_command_stores_hashes_not_text(repo: Path) -> None:
    claude_commands.record_command(ROOT, CHILD, "secret --token abc", repo)
    (rec,) = pending(repo)
    assert rec["agent"] == CHILD
    assert rec["cmd"] == cmd_hashes("secret --token abc")
    assert rec["done"] is None
    assert isinstance(rec["ts"], float)
    assert "secret" not in json.dumps(rec)


def test_record_command_ignores_nonstrings_and_missing_repos(
    repo: Path, plain_checkout: Path
) -> None:
    claude_commands.record_command(ROOT, CHILD, None, repo)
    claude_commands.record_command(ROOT, CHILD, 5, repo)
    claude_commands.record_command(ROOT, CHILD, "ls", plain_checkout)
    assert pending(repo) == []
    assert list(plain_checkout.iterdir()) == []


def test_finish_command_marks_only_the_newest_match(
    repo: Path, plain_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = itertools.count(100.0, 100.0)
    monkeypatch.setattr(time, "time", lambda: next(clock))
    claude_commands.record_command(ROOT, CHILD, "ls", repo)  # ts 100
    claude_commands.record_command(ROOT, CHILD, "ls", repo)  # ts 200
    claude_commands.record_command(ROOT, OTHER, "ls", repo)  # ts 300
    claude_commands.record_command(ROOT, CHILD, "pwd", repo)  # ts 400
    claude_commands.finish_command(ROOT, "ls", CHILD, repo)  # done at 500
    by_ts = {rec["ts"]: rec["done"] for rec in pending(repo)}
    assert by_ts == {100.0: None, 200.0: 500.0, 300.0: None, 400.0: None}
    claude_commands.finish_command(
        ROOT, "ls", CHILD, repo
    )  # now the older one, done at 600
    by_ts = {rec["ts"]: rec["done"] for rec in pending(repo)}
    assert by_ts == {100.0: 600.0, 200.0: 500.0, 300.0: None, 400.0: None}
    claude_commands.finish_command(ROOT, "ls", CHILD, repo)  # nothing left: no change
    assert {rec["ts"]: rec["done"] for rec in pending(repo)} == by_ts
    claude_commands.finish_command(ROOT, "ls", None, repo)  # the main agent has none
    claude_commands.finish_command(ROOT, "nope", OTHER, repo)
    claude_commands.finish_command(ROOT, 7, OTHER, repo)
    claude_commands.finish_command(ROOT, "ls", OTHER, plain_checkout)
    assert {rec["ts"]: rec["done"] for rec in pending(repo)} == by_ts


def test_finish_command_matches_the_main_agent(repo: Path) -> None:
    claude_commands.record_command(ROOT, None, "ls", repo)
    claude_commands.finish_command(ROOT, "ls", None, repo)
    (rec,) = pending(repo)
    assert isinstance(rec["done"], float)


def test_command_records_skip_done_and_unreadable(repo: Path) -> None:
    claude_commands.record_command(ROOT, CHILD, "ls", repo)
    claude_commands.record_command(ROOT, CHILD, "pwd", repo)
    claude_commands.finish_command(ROOT, "pwd", CHILD, repo)
    folder = repo / ".git" / "tbhb-alpha" / "cmds" / ("a" * 16)
    (folder / "junk.json").write_text("{", encoding="utf-8")
    (folder / "empty.json").write_text("{}", encoding="utf-8")
    store = repo / ".git" / "tbhb-alpha"
    got = [rec for _, rec in claude_commands.command_records(store, ROOT)]
    assert [rec["cmd"] for rec in got] == [cmd_hashes("ls")]
    assert list(claude_commands.command_records(store, OTHER)) == []


def test_store_directory_names(repo: Path, tmp_path: Path) -> None:
    store = repo / ".git" / "tbhb-alpha"
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    sessions.write_record(ROOT, {"model": "m"})
    claude_commands.record_command(ROOT, None, "ls")
    receipts.write_edit(make_ident(), str(repo / "a.txt"))
    home = tmp_path / "codex"
    write_rollout(home, ROOT_THREAD, [meta(id=ROOT_THREAD), patch_call("a.txt")])
    info = storage.repo_info()
    assert info is not None
    codex_rollout.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert sorted(os.listdir(store)) == [  # noqa: PTH208 -- exact on-disk names
        "cmds",
        "edits",
        "lock",
        "marks",
        "marks.lock",
        "sessions",
    ]
    feed_sid = records.sha256_64("fresh")
    storage.write_json(store / "sessions" / f"{records.hx(feed_sid)}.json", {})
    assert sorted(os.listdir(store / "sessions"))  # noqa: PTH208 -- exact on-disk names


def test_finish_command_prefers_the_newest_not_the_last_path(repo: Path) -> None:
    cmds = repo / ".git" / "tbhb-alpha" / "cmds" / records.hx(ROOT)
    hashes = cmd_hashes("ls")
    for name, ts in (("a.json", 20), ("z.json", 10)):
        storage.write_json(
            cmds / name, {"agent": None, "cmd": hashes, "ts": ts, "done": None}
        )
    claude_commands.finish_command(ROOT, "ls", None)
    assert loaded(cmds / "a.json")["done"] is not None
    assert loaded(cmds / "z.json")["done"] is None


def test_finish_command_ties_go_to_the_first_record(repo: Path) -> None:
    store = repo / ".git" / "tbhb-alpha"
    cmds = store / "cmds" / records.hx(ROOT)
    for name in ("one.json", "two.json"):
        storage.write_json(
            cmds / name,
            {"agent": None, "cmd": cmd_hashes("ls"), "ts": 5, "done": None},
        )
    first = next(path for path, _ in claude_commands.command_records(store, ROOT))
    claude_commands.finish_command(ROOT, "ls", None)
    done = [p for p in cmds.glob("*.json") if loaded(p)["done"] is not None]
    assert done == [first]


def test_match_command_agent_window_is_exclusive(repo: Path) -> None:
    line = "sh -c eval 'ls'"
    claude_commands.record_command(ROOT, CHILD, "ls", repo)
    edge = time_of(repo) + claude_matching.CMD_WINDOW
    assert claude_commands.match_command_agent(ROOT, [line], repo, now=edge) is None


@pytest.mark.parametrize("command", ["trailer", "stamp-docs"])
def test_unresolved_child_commit_is_blocked_with_exact_recovery_message(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    seed_claude(repo)
    stage(repo, "f.txt")
    claude_commands.record_command(SESSION_HASH, AGENT, "git commit -m x", repo)
    monkeypatch.setattr(claude_ancestry, "ancestry", lambda: ["unmatched"])
    message = msg_file(tmp_path, "fix: example\n")
    assert dispatcher.run([command, str(message)], CLAUDE_COMMIT_ENV) == 1
    assert capsys.readouterr() == (
        "",
        (
            "tbhbdev alpha identity: Attribution couldn't be resolved for a commit "
            "made while a subagent was active. Retry the commit from the main "
            "session after the subagent's command finishes.\n"
        ),
    )
    assert message.read_text() == "fix: example\n"
    assert not (repo / ".git" / storage.STORE / "claims").exists()


def test_matching_main_command_is_safe_while_a_child_command_overlaps(
    repo: Path,
) -> None:
    seed_claude(repo)
    claude_commands.record_command(SESSION_HASH, AGENT, "ls", repo)
    claude_commands.record_command(SESSION_HASH, None, "git commit -m x", repo)
    got = attribution.committer_identity(
        CLAUDE_COMMIT_ENV, ["sh -c eval 'git commit -m x'"]
    )
    assert got is not None
    assert got.ident.subagent is None
    assert got.ident.model == "anthropic:claude-opus-5-5"


@pytest.mark.parametrize("agent", [None, OTHER])
def test_ambiguous_child_commit_is_refused(repo: Path, agent: str | None) -> None:
    claude_commands.record_command(SESSION_HASH, AGENT, "git commit -m x", repo)
    claude_commands.record_command(SESSION_HASH, agent, "git commit -m x", repo)
    with pytest.raises(records.AttributionError):
        attribution.committer_identity(
            CLAUDE_COMMIT_ENV, ["sh -c eval 'git commit -m x'"]
        )


def test_command_window_includes_its_start_and_an_explicit_zero(repo: Path) -> None:
    record: Json = {"agent": CHILD, "cmd": cmd_hashes("ls"), "ts": 0.0}
    storage.write_json(
        repo / ".git" / storage.STORE / "cmds" / records.hx(ROOT) / "x.json", record
    )
    assert claude_commands.recent_commands(ROOT, repo, 0.0) == [record]
    assert (
        claude_commands.match_command_agent(ROOT, ["sh -c eval 'ls'"], repo, now=0.0)
        == CHILD
    )
    assert claude_commands.recent_commands(
        ROOT, repo, claude_matching.CMD_WINDOW - 1
    ) == [record]
    assert claude_commands.recent_commands(ROOT, repo, claude_matching.CMD_WINDOW) == []
    assert claude_commands.recent_commands(ROOT, repo, -1.0) == []
