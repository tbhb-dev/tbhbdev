# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity claude commands tests."""

import itertools
import json
import os
import stat
import time
from typing import TYPE_CHECKING, cast

import pytest
from integration.alpha.identity.identity_test_support import (
    AGENT,
    CHILD,
    CLAUDE_COMMIT_ENV,
    OTHER,
    ROOT,
    SESSION_HASH,
    H,
    init_repo,
    loaded,
    msg_file,
    pending,
    seed_claude,
    stage,
)

from tbhbdev.alpha.identity import (
    attribution,
    claude_commands,
    claude_commit,
    dispatcher,
    sessions,
    storage,
    values,
)
from tbhbdev.alpha.identity.commands import cmd_hashes

if TYPE_CHECKING:
    from pathlib import Path


def test_command_matching_and_ambiguity(repo: Path) -> None:
    def line(cmd: str) -> str:
        return f"/bin/zsh -c source s && eval '{cmd}' < /dev/null && pwd -P >| /t"

    claude_commands.record_command(ROOT, CHILD, "git commit -m x", repo)
    claude_commands.record_command(ROOT, None, "ls", repo)
    match = claude_commands.match_command_agent
    assert match(ROOT, [line("git commit -m x")], repo) == (CHILD, False)
    assert match(ROOT, [line("ls")], repo) == (None, False)
    assert match(ROOT, [line("other")], repo) == (None, False)
    assert match(ROOT, [], repo) == (None, False)
    # The harness may trim the command it runs.
    assert match(ROOT, [line("git commit -m x  ")], repo) == (CHILD, False)
    claude_commands.record_command(ROOT, CHILD, "padded  ", repo)
    assert match(ROOT, [line("padded")], repo) == (CHILD, False)
    # Identical commands from two agents are ambiguous.
    claude_commands.record_command(ROOT, OTHER, "git commit -m x", repo)
    assert match(ROOT, [line("git commit -m x")], repo) == (None, True)
    # The main agent and a subagent running the same command are ambiguous too.
    claude_commands.record_command(ROOT, None, "shared", repo)
    claude_commands.record_command(ROOT, CHILD, "shared", repo)
    assert match(ROOT, [line("shared")], repo) == (None, True)
    # Finished records stop matching, so sequential repeats are fine.
    claude_commands.finish_command(ROOT, "git commit -m x", OTHER, repo)
    assert match(ROOT, [line("git commit -m x")], repo) == (CHILD, False)
    # Other sessions never match.
    assert match(H + "9" * 16, [line("git commit -m x")], repo) == (None, False)


def test_match_command_agent_window_and_defaults(
    repo: Path, plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = "sh -c eval 'ls'"
    claude_commands.record_command(ROOT, CHILD, "ls", repo)
    assert claude_commands.match_command_agent(ROOT, [line], repo) == (CHILD, False)
    far = time_of(repo) + claude_commands.CMD_WINDOW
    assert claude_commands.match_command_agent(ROOT, [line], repo, now=far - 1) == (
        CHILD,
        False,
    )
    assert claude_commands.match_command_agent(ROOT, [line], repo, now=far + 1) == (
        None,
        False,
    )
    monkeypatch.setattr(time, "time", lambda: far + 1)
    assert claude_commands.match_command_agent(ROOT, [line], repo) == (None, False)
    monkeypatch.chdir(plain)
    assert claude_commands.match_command_agent(ROOT, [line]) == (None, False)


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
        assert claude_commands.match_command_agent(ROOT, [line], repo) == (CHILD, False)


def test_a_commit_in_another_clone_finds_the_agent(repo: Path, tmp_path: Path) -> None:
    line = "sh -c eval 'git commit -m x'"
    other = init_repo(tmp_path / "other")
    claude_commands.record_command(ROOT, CHILD, "git commit -m x", repo)
    assert claude_commands.match_command_agent(ROOT, [line], other) == (CHILD, False)
    claude_commands.finish_command(ROOT, "git commit -m x", CHILD, repo)
    assert claude_commands.match_command_agent(ROOT, [line], other) == (None, False)
    assert not (other / ".git" / "tbhb-alpha").exists()


def test_the_user_store_keeps_commands_private(repo: Path) -> None:
    old = os.umask(0)
    try:
        claude_commands.record_command(ROOT, CHILD, "ls", repo)
        user = storage.user_store()
        assert "cmds" in {child.name for child in user.iterdir()}
        (path,) = (user / "cmds" / ("a" * 16)).glob("*.json")
        assert stat.S_IMODE(user.stat().st_mode) == 0o700
        references = user / "cmd-stores" / values.hx(ROOT)
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
    repo: Path, plain: Path
) -> None:
    # A teammate runs in a session whose directory is no repository, and commits
    # in a clone: its command is found through the user store alone.
    claude_commands.record_command(ROOT, CHILD, "git commit -m x", plain)
    assert list(plain.iterdir()) == []
    assert not (storage.user_store() / "cmd-stores").exists()
    match = claude_commands.match_command_agent
    assert match(ROOT, ["sh -c eval 'git commit -m x'"], repo) == (CHILD, False)
    claude_commands.finish_command(ROOT, "git commit -m x", CHILD, plain)
    assert match(ROOT, ["sh -c eval 'git commit -m x'"], repo) == (None, False)
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
    repo: Path, plain: Path
) -> None:
    claude_commands.record_command(ROOT, CHILD, None, repo)
    claude_commands.record_command(ROOT, CHILD, 5, repo)
    claude_commands.record_command(ROOT, CHILD, "ls", plain)
    assert pending(repo) == []
    assert list(plain.iterdir()) == []


def test_finish_command_marks_only_the_newest_match(
    repo: Path, plain: Path, monkeypatch: pytest.MonkeyPatch
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
    claude_commands.finish_command(ROOT, "ls", OTHER, plain)
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


def test_finish_command_prefers_the_newest_not_the_last_path(repo: Path) -> None:
    cmds = repo / ".git" / "tbhb-alpha" / "cmds" / values.hx(ROOT)
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
    cmds = store / "cmds" / values.hx(ROOT)
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
    edge = time_of(repo) + claude_commands.CMD_WINDOW
    assert claude_commands.match_command_agent(ROOT, [line], repo, now=edge) == (
        None,
        False,
    )


def test_command_store_references_follow_the_root_and_keep_local_precedence(
    repo: Path, tmp_path: Path
) -> None:
    other = init_repo(tmp_path / "other clone")
    claude_commands.record_command(ROOT, CHILD, "ls", other)
    claude_commands.record_command(ROOT, CHILD, "pwd", repo)
    references = storage.user_store() / "cmd-stores" / values.hx(ROOT)
    storage.write_json(references / "invalid.json", {"store": 5})
    storage.write_json(references / "empty.json", {})
    remote = other / ".git" / storage.STORE
    local = repo / ".git" / storage.STORE
    assert sessions.session_stores(ROOT, repo) == [remote, local]
    assert sessions.session_stores(OTHER, repo) == [local]
    assert set(sessions.session_stores(ROOT, tmp_path)) == {remote, local}
    for store in (remote, local):
        reference = references / f"{values.hx(values.sha256_64(str(store)))}.json"
        assert loaded(reference) == {"store": str(store)}
        assert stat.S_IMODE(reference.stat().st_mode) == 0o600
    assert stat.S_IMODE(references.stat().st_mode) == 0o700


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
    monkeypatch.setattr(claude_commit, "ancestry", lambda: ["unmatched"])
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
    with pytest.raises(claude_commands.AttributionError):
        attribution.committer_identity(
            CLAUDE_COMMIT_ENV, ["sh -c eval 'git commit -m x'"]
        )


@pytest.mark.parametrize("foreign", [False, True])
def test_matched_child_requires_a_session_record_for_its_root(
    repo: Path, foreign: bool
) -> None:
    seed_claude(repo)
    claude_commands.record_command(SESSION_HASH, AGENT, "git commit -m x", repo)
    if foreign:
        sessions.write_record(AGENT, {"root": OTHER, "model": "claude-haiku-5-5"}, repo)
    with pytest.raises(claude_commands.AttributionError) as info:
        attribution.committer_identity(
            CLAUDE_COMMIT_ENV, ["sh -c eval 'git commit -m x'"]
        )
    assert str(info.value) == (
        "The subagent's session record is missing or belongs to another root. "
        "Retry after the subagent's session record is restored."
    )


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
