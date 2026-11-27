# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity claude commit tests."""

import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from integration.alpha.identity.identity_test_support import (
    AGENT,
    CHILD,
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    CLAUDE_HOOK_ENV,
    HAIKU,
    OPUS,
    OTHER,
    SESSION_HASH,
    SONNET,
    Json,
    claude_data,
    git_commit,
    init_repo,
    needs_prek,
    record,
    reply,
    run_git,
    seed_claude,
    stage,
    stdin_fixture,
    trailers,
    write_transcript,
)

from tbhbdev.alpha.identity import (
    ancestry,
    attribution,
    claude,
    claude_commands,
    claude_commit,
    claude_model,
    sessions,
    storage,
    values,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_claude_committer_main_agent(repo: Path) -> None:
    seed_claude(repo)
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, ["unrelated"])
    assert got is not None
    assert got.key == values.CLAUDE
    assert got.ident == values.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        None,
        "anthropic:claude-opus-5-5",
        "low",
        "cli",
    )
    assert got.root_ident == values.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        None,
        "anthropic:claude-opus-5-5",
        "high",
        "cli",
    )
    no_effort = {k: v for k, v in CLAUDE_COMMIT_ENV.items() if k != "CLAUDE_EFFORT"}
    again = attribution.committer_identity(no_effort, [])
    assert again is not None
    assert again.ident.effort == "high"


def test_claude_committer_uses_real_ancestry_by_default(repo: Path) -> None:
    seed_claude(repo)
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV)
    assert got is not None
    assert got.ident.subagent is None


def test_claude_committer_child_matched_by_ancestry(repo: Path) -> None:
    seed_claude(repo)
    sessions.write_record(
        AGENT,
        {"root": SESSION_HASH, "model": "claude-haiku-5-5", "effort": "medium"},
        repo,
    )
    claude_commands.record_command(SESSION_HASH, AGENT, "git commit -m x", repo)
    chain = ["/bin/zsh -c eval 'git commit -m x'"]
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, chain)
    assert got is not None
    assert got.ident == values.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        AGENT,
        "anthropic:claude-haiku-5-5",
        "medium",
        "cli",
    )
    assert got.root_ident.subagent is None
    assert got.root_ident.model == "anthropic:claude-opus-5-5"


def test_claude_committer_child_model_from_transcript(repo: Path) -> None:
    seed_claude(repo)
    transcript = repo / "child.jsonl"
    transcript.write_text(
        json.dumps({"message": {"model": "claude-haiku-5-5"}}) + "\n", encoding="utf-8"
    )
    sessions.write_record(
        AGENT, {"root": SESSION_HASH, "transcript": str(transcript)}, repo
    )
    env = {**CLAUDE_COMMIT_ENV, claude_commands.AGENT_ENV: AGENT}
    got = attribution.committer_identity(env, [])
    assert got is not None
    assert got.ident.subagent == AGENT
    assert got.ident.model == "anthropic:claude-haiku-5-5"


def test_claude_committer_rejects_foreign_agent_records(repo: Path) -> None:
    seed_claude(repo)
    sessions.write_record(AGENT, {"root": OTHER, "model": "claude-haiku-5-5"}, repo)
    for bad in (AGENT, CHILD):
        with pytest.raises(claude_commands.AttributionError):
            attribution.committer_identity(
                {**CLAUDE_COMMIT_ENV, claude_commands.AGENT_ENV: bad}, []
            )
    # A malformed agent id is ignored.
    for bad in ("not-a-hash", ""):
        got = attribution.committer_identity(
            {**CLAUDE_COMMIT_ENV, claude_commands.AGENT_ENV: bad}, []
        )
        assert got is not None
        assert got.ident.subagent is None


def test_claude_committer_version_falls_back_to_the_record(repo: Path) -> None:
    seed_claude(repo)
    got = attribution.committer_identity({"CLAUDE_CODE_SESSION_ID": "session-raw"}, [])
    assert got is not None
    assert got.ident.harness == "claude-code:2.1.316"
    assert got.ident.surface is None


def test_claude_committer_prefers_the_env_version(repo: Path) -> None:
    sessions.write_record(SESSION_HASH, {"version": "1.0"}, repo)
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, [])
    assert got is not None
    assert got.root_ident.harness == "claude-code:2.1.316"


def test_claude_committer_takes_the_main_model_at_commit_time(repo: Path) -> None:
    transcript = write_transcript(repo / "t.jsonl", reply(OPUS))
    sessions.write_record(
        SESSION_HASH,
        {"transcript": str(transcript), "model": HAIKU, "switch_model": SONNET},
        repo,
    )
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, [])
    assert got is not None
    assert got.ident.model == got.root_ident.model == "anthropic:" + OPUS
    sessions.write_record(SESSION_HASH, {"switch_at": time.time()}, repo)
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, [])
    assert got is not None
    assert got.ident.model == got.root_ident.model == "anthropic:" + SONNET


@pytest.mark.usefixtures("repo")
def test_claude_committer_reads_the_model_from_the_session_transcript(
    tmp_path: Path,
) -> None:
    # A run disables hooks, so no session record names the transcript.
    config = tmp_path / "config"
    project = config / "projects" / "example-project"
    project.mkdir(parents=True)
    write_transcript(project / "session-raw.jsonl", reply(SONNET))
    write_transcript(config / "projects" / "session-raw.jsonl", reply(OPUS))
    env = {**CLAUDE_COMMIT_ENV, "CLAUDE_CONFIG_DIR": str(config)}
    got = attribution.committer_identity(env, [])
    assert got is not None
    assert got.ident.model == got.root_ident.model == "anthropic:" + SONNET
    home = {**CLAUDE_COMMIT_ENV, "HOME": str(tmp_path / "config" / "..")}
    assert claude_model.session_model("session-raw", home) is None
    empty = project / "session-empty.jsonl"
    write_transcript(empty, {"type": "user"})
    assert claude_model.session_model("session-empty", env) is None
    assert (
        claude_model.session_model("session-missing", {"HOME": str(tmp_path)}) is None
    )


def main_hook(repo: Path, event: str, transcript: Path, **kw: object) -> Json:
    """A main-agent hook payload without a model, as Claude Code sends."""
    return stdin_fixture(
        "claude-pretooluse-bash-main.json",
        cwd=str(repo),
        transcript_path=str(transcript),
        hook_event_name=event,
        **kw,
    )


@needs_prek
def test_e2e_a_headless_first_turn_commit_gets_the_transcript_model(
    wired: Path,
) -> None:
    transcript = wired.parent / "session.jsonl"
    write_transcript(transcript, {"type": "user", "message": {"content": "go"}})
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
        record(wired, "claude", main_hook(wired, event, transcript), CLAUDE_E2E)
    assert "model" not in sessions.load_record(SESSION_HASH, wired)
    # The reply that runs the Bash call lands before its git hooks run.
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(reply(OPUS)) + "\n")
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    (line,) = trailers(wired)
    assert f";model=anthropic:{OPUS};" in line


def switch_then_turn(wired: Path, transcript: Path) -> None:
    """The measured /model sequence: a switch, then a turn the transcript trails."""
    write_transcript(transcript, reply(OPUS))
    record(wired, "claude", main_hook(wired, "SessionStart", transcript), CLAUDE_E2E)
    for event in ("PreModelSwitch", "PostModelSwitch"):
        data = main_hook(wired, event, transcript, from_model=OPUS, to_model=SONNET)
        record(wired, "claude", data, CLAUDE_E2E)
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
        record(wired, "claude", main_hook(wired, event, transcript), CLAUDE_E2E)


@needs_prek
def test_e2e_a_commit_after_a_model_switch_gets_the_new_model(wired: Path) -> None:
    transcript = wired.parent / "session.jsonl"
    switch_then_turn(wired, transcript)
    assert claude_model.transcript_model(str(transcript)) == OPUS
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    (line,) = trailers(wired)
    assert f";model=anthropic:{SONNET};" in line


@needs_prek
def test_e2e_a_reply_newer_than_the_switch_wins(wired: Path) -> None:
    transcript = wired.parent / "session.jsonl"
    switch_then_turn(wired, transcript)
    later = datetime.fromtimestamp(time.time() + 60, UTC).isoformat()
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(reply(HAIKU, later)) + "\n")
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    (line,) = trailers(wired)
    assert f";model=anthropic:{HAIKU};" in line


@needs_prek
def test_e2e_a_commit_without_any_model_fails(wired: Path) -> None:
    transcript = wired.parent / "session.jsonl"
    write_transcript(transcript, {"type": "user", "message": {"content": "go"}})
    record(wired, "claude", main_hook(wired, "SessionStart", transcript), CLAUDE_E2E)
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode != 0
    assert ": model required" in done.stderr
    assert run_git(wired, "log", "-1", "--format=%s") == "init\n"


def test_claude_committer_finds_records_only_in_another_clone(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = init_repo(tmp_path / "other clone")
    seed_claude(other)
    data = claude_data(other, "claude-pretooluse-bash-child.json")
    data["tool_input"] = {"command": "git commit -m x"}
    claude.handle_claude(data, CLAUDE_HOOK_ENV)
    sessions.write_record(AGENT, {"model": "claude-haiku-5-5"}, other)
    user = storage.user_store()
    (user / "cmds").rename(tmp_path / "saved-commands")
    (user / f"{values.hx(AGENT)}.json").rename(tmp_path / "saved-child.json")
    (user / f"{values.hx(SESSION_HASH)}.json").rename(tmp_path / "saved-root.json")
    assert not (repo / ".git" / storage.STORE / "cmds").exists()
    monkeypatch.setattr(
        claude_commit, "ancestry", lambda: ["sh -c eval 'git commit -m x'"]
    )
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV)
    assert got is not None
    assert got.ident.subagent == AGENT
    assert got.ident.effort == "high"
    assert got.ident.model == "anthropic:claude-haiku-5-5"
    assert got.root_ident.model == "anthropic:claude-opus-5-5"
    assert sessions.load_record(AGENT, root=SESSION_HASH)["root"] == SESSION_HASH
    claude_commands.finish_command(SESSION_HASH, "git commit -m x", AGENT, other)
    assert claude_commands.match_command_agent(
        SESSION_HASH, ancestry.ancestry(), strict=True
    ) == (
        None,
        False,
    )


def test_claude_committer_reads_the_root_project_when_user_state_writes_fail(
    repo: Path, tmp_path: Path
) -> None:
    other = init_repo(tmp_path / "root project")
    storage.user_store().parent.mkdir(parents=True)
    storage.user_store().write_text("not a directory")
    seed_claude(other)
    data = claude_data(other, "claude-pretooluse-bash-child.json")
    data["tool_input"] = {"command": "git commit -m x"}
    claude.handle_claude(data, CLAUDE_HOOK_ENV)
    sessions.write_record(AGENT, {"model": "claude-haiku-5-5"}, other)
    env = {**CLAUDE_COMMIT_ENV, "CLAUDE_PROJECT_DIR": str(other)}
    got = attribution.committer_identity(env, ["sh -c eval 'git commit -m x'"])
    assert got is not None
    assert got.ident.subagent == AGENT
    assert got.ident.model == "anthropic:claude-haiku-5-5"
    assert got.root_ident.model == "anthropic:claude-opus-5-5"
    with pytest.raises(claude_commands.AttributionError):
        attribution.committer_identity(env, ["unmatched"])
    assert storage.user_store().read_text() == "not a directory"
    assert not (repo / ".git" / storage.STORE).exists()
