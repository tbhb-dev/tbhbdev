# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity record tests."""

import os
import subprocess
import time
from typing import TYPE_CHECKING

import pytest
from integration.alpha.identity.identity_test_support import (
    CLAUDE_COMMIT_ENV,
    CLAUDE_HOOK_ENV,
    CONV,
    HAIKU,
    OPUS,
    SESSION_HASH,
    SONNET,
    claude_data,
    feed,
    init_repo,
    loaded,
    msg_file,
    no_start,
    pending,
    reply,
    returning,
    run_git,
    seed_claude,
    stage,
    write_transcript,
)

from tbhbdev.alpha.identity import commit as identity_commit
from tbhbdev.alpha.identity import record as identity_record
from tbhbdev.alpha.identity import sessions, storage, validation, values
from tbhbdev.alpha.identity import trailers as identity_trailers

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("harness", ["claude", "codex", "antigravity"])
def test_record_outside_tbhb_dev_keeps_the_repository_untouched(
    repo: Path,
    harness: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_git(repo, "remote", "set-url", "origin", "git@github.com:someone/x.git")
    feed(monkeypatch, {"session_id": "s", "model": "m", "cwd": str(repo)})
    assert identity_record.cmd_record(harness, {"CODEX_SESSION_ID": "s"}) == 0
    assert capsys.readouterr() == ("", "")
    assert not (repo / ".git" / "tbhb-alpha").exists()
    user_record = storage.user_store() / f"{values.hx(values.sha256_64('s'))}.json"
    if harness == "claude":
        assert loaded(user_record)["payload_model"] == "m"
    else:
        assert not user_record.exists()


def test_cmd_record_claude_dispatch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert identity_record.cmd_record("nonsense", {}) == 0
    feed(monkeypatch, claude_data(repo))
    assert identity_record.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    assert sessions.load_record(SESSION_HASH)["harness"] == "claude-code"
    assert len(pending(repo)) == 1
    # Without a session ID nothing is recorded.
    feed(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert identity_record.cmd_record("claude", {}) == 0
    assert len(pending(repo)) == 1
    # session_id spellings are normalized.
    data = claude_data(repo)
    del data["session_id"]
    feed(monkeypatch, {**data, "sessionId": "session-raw"})
    assert identity_record.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    assert len(pending(repo)) == 2


def test_cmd_record_session_from_env_and_filters(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"model": "<synthetic>", "effort": {"level": "high"}})
    assert (
        identity_record.cmd_record("antigravity", {"ANTIGRAVITY_CONVERSATION_ID": CONV})
        == 0
    )
    rec = sessions.load_record(values.sha256_64(CONV))
    assert no_start(rec) == {"harness": "antigravity", "effort": "high"}
    feed(monkeypatch, {"model": "gpt-5.6-sol", "session_id": CONV})
    assert identity_record.cmd_record("antigravity", {}) == 0
    assert no_start(sessions.load_record(values.sha256_64(CONV))) == {
        "harness": "antigravity",
        "effort": "high",
        "model": "gpt-5.6-sol",
    }
    # No session anywhere, or no repository: nothing is written.
    feed(monkeypatch, {"model": "m"})
    assert identity_record.cmd_record("antigravity", {}) == 0
    assert len(list((repo / ".git" / "tbhb-alpha" / "sessions").glob("*"))) == 1


def test_cmd_record_outside_a_repository(
    plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert identity_record.cmd_record("antigravity", {}) == 0
    assert list(plain.iterdir()) == []


def test_cmd_record_claude_outside_a_repository_keeps_a_user_record(
    plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m", "cwd": str(plain)})
    assert identity_record.cmd_record("claude", {}) == 0
    assert list(plain.iterdir()) == []
    assert sessions.load_record(values.sha256_64("s"))["payload_model"] == "m"


def test_cmd_record_uses_the_hook_cwd_outside_a_repository(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    feed(monkeypatch, {"session_id": "s", "model": "m", "cwd": str(outside)})
    assert identity_record.cmd_record("claude", {}) == 0
    assert not (repo / ".git" / "tbhb-alpha" / "sessions").exists()
    assert (storage.user_store() / f"{values.hx(values.sha256_64('s'))}.json").is_file()


@pytest.mark.parametrize("event", ["SessionStart", "PostModelSwitch"])
def test_cmd_record_uses_the_hook_cwd_in_another_owners_repository(
    plain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    foreign = init_repo(tmp_path / "foreign")
    run_git(foreign, "remote", "set-url", "origin", "git@github.com:someone/x.git")
    transcript = write_transcript(tmp_path / "session.jsonl", reply(OPUS, None))
    status = run_git(foreign, "status", "--porcelain")
    entries = sorted((foreign / ".git").iterdir())
    monkeypatch.setattr(time, "time", returning(123.0))
    data = {
        "session_id": "s",
        "model": HAIKU,
        "effort": "high",
        "cwd": str(foreign),
        "transcript_path": str(transcript),
        "hook_event_name": event,
        "to_model": SONNET,
    }
    feed(monkeypatch, data)
    assert identity_record.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    expected: dict[str, object] = {
        "harness": "claude-code",
        "version": "2.1.316",
        "model": OPUS,
        "effort": "high",
        "transcript": str(transcript),
        "payload_model": HAIKU,
    }
    if event == "PostModelSwitch":
        expected.update(switch_model=SONNET, switch_at=123.0)
    user_record = storage.user_store() / f"{values.hx(values.sha256_64('s'))}.json"
    assert no_start(loaded(user_record)) == expected
    # Missing values in later hooks must preserve a known model and switch.
    feed(monkeypatch, {"session_id": "s", "cwd": str(foreign)})
    assert identity_record.cmd_record("claude", {}) == 0
    assert no_start(loaded(user_record)) == expected
    assert not (foreign / ".git" / "tbhb-alpha").exists()
    assert run_git(foreign, "status", "--porcelain") == status
    assert sorted((foreign / ".git").iterdir()) == entries
    assert list(plain.iterdir()) == []


def test_git_init_in_a_plain_directory_finishes_its_user_record(
    plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The directory is no repository before the command and another owner's
    # repository after it: the finish must still reach the user's store alone.
    data = {
        "session_id": "s",
        "cwd": str(plain),
        "tool_name": "Bash",
        "tool_input": {"command": "git init"},
    }
    feed(monkeypatch, {**data, "hook_event_name": "PreToolUse"})
    assert identity_record.cmd_record("claude", {}) == 0
    subprocess.run(["git", "init", "-q", str(plain)], check=True)
    before = sorted((plain / ".git").iterdir())
    feed(monkeypatch, {**data, "hook_event_name": "PostToolUse"})
    assert identity_record.cmd_record("claude", {}) == 0
    (path,) = (storage.user_store() / "cmds" / values.hx(values.sha256_64("s"))).glob(
        "*.json"
    )
    assert loaded(path)["done"] is not None
    assert sorted((plain / ".git").iterdir()) == before


@pytest.mark.parametrize("agent", [None, "child"])
@pytest.mark.parametrize("post", ["PostToolUse", "PostToolUseFailure"])
def test_git_init_in_a_plain_directory_finishes_each_agents_record(
    plain: Path, monkeypatch: pytest.MonkeyPatch, agent: str | None, post: str
) -> None:
    data = {
        "session_id": "s",
        "agent_id": agent,
        "agent_type": "general-purpose" if agent else None,
        "cwd": str(plain),
        "tool_name": "Bash",
        "tool_input": {"command": "git init"},
    }
    feed(monkeypatch, {**data, "hook_event_name": "PreToolUse"})
    assert identity_record.cmd_record("claude", {}) == 0
    subprocess.run(["git", "init", "-q", str(plain)], check=True)
    feed(monkeypatch, {**data, "hook_event_name": post})
    assert identity_record.cmd_record("claude", {}) == 0
    (path,) = (storage.user_store() / "cmds" / values.hx(values.sha256_64("s"))).glob(
        "*.json"
    )
    rec = loaded(path)
    assert rec["done"] is not None
    assert rec["agent"] == (values.sha256_64(agent) if agent else None)


@pytest.mark.parametrize("tool", ["Bash", "Edit"])
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
@pytest.mark.parametrize("agent", [None, "child"])
def test_foreign_hooks_keep_no_commands_edits_or_subagent_records(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    event: str,
    agent: str | None,
) -> None:
    foreign = init_repo(repo.parent / "foreign")
    run_git(foreign, "remote", "set-url", "origin", "git@github.com:someone/x.git")
    target = repo / "edited.txt"
    target.write_text("edit\n", encoding="utf-8")
    feed(
        monkeypatch,
        {
            "session_id": "s",
            "agent_id": agent,
            "cwd": str(foreign),
            "hook_event_name": event,
            "tool_name": tool,
            "tool_input": {"command": "git commit -m x", "file_path": str(target)},
            "tool_response": {"bashEditDiff": [{"filePath": str(target)}]},
        },
    )
    assert identity_record.cmd_record("claude", {}) == 0
    assert not (foreign / ".git" / "tbhb-alpha").exists()
    assert not (repo / ".git" / "tbhb-alpha").exists()
    if agent:
        assert not storage.user_store().exists()
    else:
        assert sorted(p.name for p in storage.user_store().rglob("*")) == [
            f"{values.hx(values.sha256_64('s'))}.json",
            "lock",
        ]


@pytest.mark.usefixtures("repo")
def test_cmd_record_brain_root_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    feed(monkeypatch, {"session_id": "s"})
    identity_record.cmd_record("antigravity", {})
    assert "brain_root" not in sessions.load_record(values.sha256_64("s"))
    feed(monkeypatch, {"session_id": "s", "artifactDirectoryPath": "/x/brain/s"})
    identity_record.cmd_record("antigravity", {})
    assert sessions.load_record(values.sha256_64("s"))["brain_root"] == "/x/brain"


def test_foreign_root_session_supplies_the_commit_trailer_model(
    repo: Path, plain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_claude(plain)
    foreign = init_repo(tmp_path / "foreign")
    run_git(foreign, "remote", "set-url", "origin", "git@github.com:someone/x.git")
    transcript = write_transcript(
        tmp_path / "session.jsonl", reply(OPUS, None), reply(SONNET, None)
    )
    feed(
        monkeypatch,
        {
            "session_id": "session-raw",
            "cwd": str(foreign),
            "transcript_path": str(transcript),
        },
    )
    assert identity_record.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    monkeypatch.chdir(repo)
    stage(repo, "a.txt")
    path = msg_file(tmp_path, "fix: record foreign session\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert no_start(validation.llm_trailers(path.read_text(encoding="utf-8"))) == [
        (
            identity_trailers.KEY_GEN,
            (
                f"LLM effort=low;harness=claude-code:2.1.316;"
                f"model=anthropic:{SONNET};session={SESSION_HASH};surface=cli"
            ),
        )
    ]
    assert not (foreign / ".git" / "tbhb-alpha").exists()


def test_cmd_record_writes_the_sessions_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert identity_record.cmd_record("antigravity", {}) == 0
    assert sorted(os.listdir(repo / ".git" / "tbhb-alpha")) == ["lock", "sessions"]  # noqa: PTH208 -- exact on-disk names
