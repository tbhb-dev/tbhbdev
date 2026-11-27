# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity storage tests."""

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from integration.alpha.identity.identity_test_support import (
    CHILD,
    GOOD,
    NO_SESSION_MESSAGE,
    ROOT,
    FakeRun,
    Json,
    add_worktree,
    hook_env,
    init_repo,
    loaded,
    msg_file,
    pending,
    spy_dirs,
)

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import (
    claude_commands,
    dispatcher,
    edits,
    sessions,
    storage,
    values,
)
from tbhbdev.alpha.identity.commands import cmd_hashes


def test_read_record_defaults(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    listy = tmp_path / "list.json"
    listy.write_text("[1]", encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert storage.read_record(good) == {"a": 1}
    for path in (listy, bad, tmp_path / "missing.json"):
        assert storage.read_record(path) == {}


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
        assert storage.read_record(path) == obj


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


def test_state_dir_and_repo_info(
    repo: Path, plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    store = repo / ".git" / "tbhb-alpha"
    assert storage.state_dir() == store
    assert storage.state_dir(repo) == store
    assert storage.state_dir(plain) is None
    assert storage.stores(plain) == []
    assert storage.repo_info() == storage.Repo(repo, store, store)
    assert storage.repo_info(repo) == storage.Repo(repo, store, store)
    assert storage.repo_info(plain) is None


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


def test_a_user_store_it_cant_write_keeps_the_command_records(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.user_store().parent.mkdir(parents=True)
    storage.user_store().write_text("not a directory")
    claude_commands.record_command(ROOT, CHILD, "ls", repo)
    monkeypatch.chdir(tmp_path)
    assert claude_commands.match_command_agent(ROOT, ["sh -c eval 'ls'"], repo) == (
        CHILD,
        False,
    )
    claude_commands.finish_command(ROOT, "ls", CHILD, repo)
    (rec,) = pending(repo)
    assert rec["agent"] == CHILD
    assert rec["done"] is not None
    assert storage.user_store().read_text() == "not a directory"


@pytest.mark.parametrize(
    "roles", [("AUTHOR", "COMMITTER"), ("AUTHOR",), ("COMMITTER",)]
)
@pytest.mark.parametrize(
    "text",
    [
        "subject\n\nbody\n",
        f"subject\n\nAssisted-by: LLM {GOOD}\n",
        "subject\n\nGenerated-by impostor\n",
    ],
)
def test_check_refuses_agent_email_without_session(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    roles: tuple[str, ...],
    text: str,
) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Another name")
        monkeypatch.setenv(
            f"GIT_{role}_EMAIL",
            "agent@tonyburns.net" if role in roles else "human@example.invalid",
        )
    path = msg_file(repo, text)
    assert not (repo / ".git" / storage.STORE).exists()
    assert not storage.user_store().exists()
    assert dispatcher.main(["check", str(path)], dict(os.environ)) == 1
    assert capsys.readouterr() == ("", NO_SESSION_MESSAGE + "\n")
    assert path.read_text() == text


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


def test_store_is_read_from_the_same_names(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = repo / ".git" / "tbhb-alpha"
    info = storage.repo_info()
    assert info is not None
    seen = spy_dirs(monkeypatch)
    list(claude_commands.command_records(store, ROOT))
    edits.load_edits(info, ROOT)
    assert seen == [store / "cmds" / values.hx(ROOT), store / "edits" / values.hx(ROOT)]
    seen.clear()
    storage.stores()
    assert seen == [repo / ".git" / "worktrees"]
    paths: list[Path] = []

    def spy(path: Path) -> Json:
        paths.append(path)
        return {}

    monkeypatch.setattr(sessions, "read_record", spy)
    sessions.load_record(ROOT)
    assert paths == [
        storage.user_store() / f"{values.hx(ROOT)}.json",
        store / "sessions" / f"{values.hx(ROOT)}.json",
    ]


def test_command_window_includes_its_start_and_an_explicit_zero(repo: Path) -> None:
    record: Json = {"agent": CHILD, "cmd": cmd_hashes("ls"), "ts": 0.0}
    storage.write_json(
        repo / ".git" / storage.STORE / "cmds" / values.hx(ROOT) / "x.json", record
    )
    assert claude_commands.recent_commands(ROOT, repo, 0.0) == [record]
    assert claude_commands.match_command_agent(
        ROOT, ["sh -c eval 'ls'"], repo, now=0.0
    ) == (
        CHILD,
        False,
    )
    assert claude_commands.recent_commands(
        ROOT, repo, claude_commands.CMD_WINDOW - 1
    ) == [record]
    assert claude_commands.recent_commands(ROOT, repo, claude_commands.CMD_WINDOW) == []
    assert claude_commands.recent_commands(ROOT, repo, -1.0) == []
