# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity sessions."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import (
    CHILD,
    EARLY,
    LATE,
    LAUNCH,
    OTHER,
    ROOT,
    SESSION_HASH,
    Json,
)
from unit.alpha.identity.identity_fixtures import (
    NOW,
    SRC,
    add_worktree,
    hook_env,
    init_repo,
    loaded,
    returning,
    session_file,
)

from tbhbdev.alpha.identity import records, sessions, storage
from tbhbdev.alpha.identity.claude import hooks as claude_hooks

RECORD_WRITER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tbhbdev.alpha.identity.sessions import write_record
for i in range(20):
    write_record(sys.argv[3], {f"{sys.argv[4]}-{i}": "v"}, Path(sys.argv[2]))
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


def test_records_honor_the_cwd_argument(repo: Path, plain_checkout: Path) -> None:
    # `plain` is the working directory; `repo` is only reachable by argument.
    sessions.write_record(ROOT, {"model": "m"}, repo)
    assert sessions.load_record(ROOT, repo) == {"model": "m"}
    assert loaded(repo / ".git" / "tbhb-alpha" / "sessions" / f"{'a' * 16}.json")
    assert list(plain_checkout.iterdir()) == []


def test_a_session_is_known_in_another_clone(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.write_record(ROOT, {"model": "m", "effort": "low"}, repo)
    other = init_repo(tmp_path / "other")
    monkeypatch.chdir(other)
    assert sessions.load_record(ROOT) == {"model": "m", "effort": "low"}
    assert sessions.load_record(ROOT, other) == {"model": "m", "effort": "low"}
    assert not (other / ".git" / "tbhb-alpha").exists()


def test_load_record_takes_the_latest_switch_as_a_pair(
    repo: Path, tmp_path: Path
) -> None:
    tree = add_worktree(repo, tmp_path / "wt")
    user = storage.user_store() / f"{records.hx(SESSION_HASH)}.json"
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


NOW_UTC = "2026-11-14T00:00:00Z"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", returning(NOW))


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


def test_claim_launch_goes_to_the_first_root_session(launch_file: Path) -> None:
    fields = {"launch": LAUNCH, "started_at": EARLY}
    assert sessions.claim_launch(ROOT) == fields
    assert loaded(launch_file) == {**fields, "session": ROOT}
    assert not list(launch_file.parent.glob("*.tmp"))
    assert sessions.claim_launch(OTHER) == {}
    assert sessions.claim_launch(ROOT) == fields
    assert loaded(launch_file) == {**fields, "session": ROOT}


@pytest.mark.parametrize(
    "content",
    [
        {"launch": LAUNCH},
        {"started_at": EARLY},
        {"launch": "nope", "started_at": EARLY},
        {"launch": LAUNCH, "started_at": "2026-02-30T00:00:00Z"},
    ],
)
def test_claim_launch_leaves_an_invalid_file_alone(
    launch_file: Path, content: Json
) -> None:
    launch_file.write_text(json.dumps(content))
    assert sessions.claim_launch(ROOT) == {}
    assert loaded(launch_file) == content


def test_claim_launch_without_a_file_or_a_repository(
    repo: Path, plain_checkout: Path
) -> None:
    assert sessions.claim_launch(ROOT) == {}
    assert sessions.claim_launch(ROOT, plain_checkout) == {}
    assert sessions.claim_launch(ROOT, repo) == {}
    assert not (repo / ".git" / "tbhb-alpha" / "launch.json").exists()


@pytest.mark.usefixtures("clock")
def test_start_fields_for_a_root_and_a_subagent(launch_file: Path) -> None:
    fields: dict[str, object] = {"launch": LAUNCH, "started_at": EARLY}
    assert sessions.start_fields(ROOT, ROOT) == fields
    sessions.write_record(ROOT, {}, once=fields)
    assert sessions.start_fields(ROOT, ROOT) == {}
    # A later session finds the file claimed and starts at its own first event.
    assert sessions.start_fields(OTHER, OTHER) == {"started_at": NOW_UTC}
    assert sessions.start_fields(CHILD, ROOT) == {
        "started_at": NOW_UTC,
        "launch": LAUNCH,
    }
    assert sessions.start_fields(CHILD, OTHER) == {
        "started_at": NOW_UTC,
        "launch": None,
    }
    sessions.write_record(CHILD, {"root": ROOT}, once={"started_at": LATE})
    assert sessions.start_fields(CHILD, ROOT) == {}
    assert loaded(launch_file)["session"] == ROOT


@pytest.mark.usefixtures("clock")
def test_start_fields_claims_the_launch_for_a_root_that_began_elsewhere(
    launch_file: Path,
) -> None:
    sessions.write_record(ROOT, {}, once={"started_at": LATE})
    assert sessions.start_fields(ROOT, ROOT) == {"launch": LAUNCH}
    assert loaded(launch_file)["session"] == ROOT
    # A subagent or a root that has its launch asks for nothing more.
    sessions.write_record(CHILD, {"root": ROOT}, once={"started_at": LATE})
    assert sessions.start_fields(CHILD, ROOT) == {}
    sessions.write_record(ROOT, {}, once={"launch": LAUNCH})
    assert sessions.start_fields(ROOT, ROOT) == {}
    # A start that is not a valid time counts as no start.
    sessions.write_record(OTHER, {}, once={"started_at": "0"})
    assert sessions.start_fields(OTHER, OTHER) == {"started_at": NOW_UTC}


def claude_event(repo: Path, session: str, agent: str | None = None) -> None:
    data: Json = {"session_id": session, "hook_event_name": "SessionStart"}
    claude_hooks.handle_claude(
        {**data, "cwd": str(repo), **({"agent_id": agent} if agent else {})}, {}
    )


def test_claude_hooks_copy_the_launch_fields_once(
    repo: Path, launch_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", returning(NOW))
    first, later = records.sha256_64("one"), records.sha256_64("two")
    claude_event(repo, "one")
    assert loaded(launch_file)["session"] == first
    monkeypatch.setattr(time, "time", returning(NOW + 60))
    claude_event(repo, "one")  # a resumed session keeps its first values
    rec = sessions.load_record(first)
    assert (rec["launch"], rec["started_at"]) == (LAUNCH, EARLY)
    claude_event(repo, "two")
    assert sessions.load_record(later)["started_at"] == "2026-11-14T00:01:00Z"
    assert "launch" not in sessions.load_record(later)
    claude_event(repo, "one", "kid")
    monkeypatch.setattr(time, "time", returning(NOW + 120))
    claude_event(repo, "one", "kid")
    kid = sessions.load_record(records.sha256_64("kid"))
    assert (kid["launch"], kid["started_at"]) == (LAUNCH, "2026-11-14T00:01:00Z")
    assert loaded(launch_file)["session"] == first


@pytest.mark.usefixtures("repo", "clock")
def test_an_interactive_session_starts_at_its_first_hook_event() -> None:
    claude_event(Path.cwd(), "one")
    rec = sessions.load_record(records.sha256_64("one"))
    assert rec["started_at"] == NOW_UTC
    assert "launch" not in rec
