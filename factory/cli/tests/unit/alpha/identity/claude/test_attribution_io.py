# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity claude attribution."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from typing import cast
lazy from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import (
    CHILD,
    HAIKU,
    OPUS,
    OTHER,
    SESSION_HASH,
    SONNET,
    VERSIONED_MAIN,
    Json,
)
from unit.alpha.identity.identity_fixtures import (
    AGENT,
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    CLAUDE_HOOK_ENV,
    claude_data,
    claude_write,
    hook_env,
    init_repo,
    last_message,
    needs_prek,
    record,
    reply,
    run_git,
    seed_claude,
    stdin_fixture,
    trailers,
    write_transcript,
)

from tbhbdev.alpha.identity import attribution, models, records, sessions, storage
from tbhbdev.alpha.identity.claude import ancestry as claude_ancestry
from tbhbdev.alpha.identity.claude import commands as claude_commands
from tbhbdev.alpha.identity.claude import hooks as claude_hooks
from tbhbdev.alpha.identity.claude import logs as claude_logs
from tbhbdev.alpha.identity.claude import matching as claude_matching
from tbhbdev.alpha.identity.commit import grammar as commit_grammar


def test_claude_committer_main_agent(repo: Path) -> None:
    seed_claude(repo)
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, ["unrelated"])
    assert got is not None
    assert got.key == models.CLAUDE
    assert got.ident == models.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        None,
        "anthropic:claude-opus-5-5",
        "low",
        "cli",
    )
    assert got.root_ident == VERSIONED_MAIN
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
    assert got.ident == models.Ident(
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
    claude_commands.record_command(SESSION_HASH, AGENT, "git commit -m x", repo)
    got = attribution.committer_identity(
        CLAUDE_COMMIT_ENV, ["sh -c eval 'git commit -m x'"]
    )
    assert got is not None
    assert got.ident.subagent == AGENT
    assert got.ident.model == "anthropic:claude-haiku-5-5"


def test_claude_committer_rejects_foreign_agent_records(repo: Path) -> None:
    seed_claude(repo)
    sessions.write_record(AGENT, {"root": OTHER, "model": "claude-haiku-5-5"}, repo)
    for bad in (AGENT, CHILD):
        claude_commands.record_command(SESSION_HASH, bad, f"git commit -m {bad}", repo)
        with pytest.raises(records.AttributionError):
            attribution.committer_identity(
                CLAUDE_COMMIT_ENV, [f"sh -c eval 'git commit -m {bad}'"]
            )


def test_claude_committer_version_falls_back_to_the_record(repo: Path) -> None:
    seed_claude(repo)
    got = attribution.committer_identity({"CLAUDE_CODE_SESSION_ID": "session-raw"}, [])
    assert got is not None
    assert got.ident.harness == "claude-code:2.1.316"
    assert got.ident.surface is None


@needs_prek
def test_e2e_claude_subagent_commits_its_parents_edit(wired: Path) -> None:
    claude_write(wired, "a.txt")
    child_data = stdin_fixture(
        "claude-pretooluse-bash-child.json",
        cwd=str(wired),
        transcript_path=str(wired / "t.jsonl"),
    )
    write_transcript(
        wired / "t" / "subagents" / "agent-agent-raw.jsonl",
        {"message": {"model": "claude-haiku-5-5"}},
    )
    record(wired, "claude", child_data, CLAUDE_E2E)
    run_git(wired, "add", "a.txt")
    agent = records.sha256_64("agent-raw")
    command = "git commit -qm 'feat: a'"
    claude_commands.finish_command(
        SESSION_HASH, cast("Json", child_data["tool_input"])["command"], agent, wired
    )
    claude_commands.record_command(SESSION_HASH, agent, command, wired)
    done = subprocess.run(
        ["sh", "-c", f"eval {shlex.quote(command)} ; status=$?; exit $status"],
        capture_output=True,
        text=True,
        check=False,
        cwd=wired,
        env=hook_env(CLAUDE_E2E),
    )
    assert done.returncode == 0, done.stderr
    session = records.sha256_64("session-raw")
    gen, assist = trailers(wired)
    assert gen.startswith("Generated-by: LLM ")
    assert f"session={session}" in gen
    assert "subagent" not in gen
    assert assist.startswith("Assisted-by: LLM ")
    assert f"subagent={agent}" in assist
    assert commit_grammar.check_message(last_message(wired)) == []


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
    assert claude_logs.session_model("session-raw", home) is None
    empty = project / "session-empty.jsonl"
    write_transcript(empty, {"type": "user"})
    assert claude_logs.session_model("session-empty", env) is None
    assert claude_logs.session_model("session-missing", {"HOME": str(tmp_path)}) is None


def test_claude_committer_finds_records_only_in_another_clone(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = init_repo(tmp_path / "other clone")
    seed_claude(other)
    data = claude_data(other, "claude-pretooluse-bash-child.json")
    data["tool_input"] = {"command": "git commit -m x"}
    claude_hooks.handle_claude(data, CLAUDE_HOOK_ENV)
    sessions.write_record(AGENT, {"model": "claude-haiku-5-5"}, other)
    user = storage.user_store()
    (user / "cmds").rename(tmp_path / "saved-commands")
    (user / f"{records.hx(AGENT)}.json").rename(tmp_path / "saved-child.json")
    (user / f"{records.hx(SESSION_HASH)}.json").rename(tmp_path / "saved-root.json")
    assert not (repo / ".git" / storage.STORE / "cmds").exists()
    monkeypatch.setattr(
        claude_ancestry, "ancestry", lambda: ["sh -c eval 'git commit -m x'"]
    )
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV)
    assert got is not None
    assert got.ident.subagent == AGENT
    assert got.ident.effort == "high"
    assert got.ident.model == "anthropic:claude-haiku-5-5"
    assert got.root_ident.model == "anthropic:claude-opus-5-5"
    assert sessions.load_record(AGENT, root=SESSION_HASH)["root"] == SESSION_HASH
    claude_commands.finish_command(SESSION_HASH, "git commit -m x", AGENT, other)
    assert (
        claude_commands.match_command_agent(SESSION_HASH, claude_ancestry.ancestry())
        is None
    )


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
                time, "time", lambda: 10000.0 + claude_matching.CMD_WINDOW
            )
        elif kind == "future":
            monkeypatch.setattr(time, "time", lambda: 9999.0)
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, ["unmatched"])
    assert got is not None
    assert got.ident.subagent is None
    assert got.ident.model == "anthropic:claude-opus-5-5"


@pytest.mark.parametrize("foreign", [False, True])
def test_matched_child_requires_a_session_record_for_its_root(
    repo: Path, foreign: bool
) -> None:
    seed_claude(repo)
    claude_commands.record_command(SESSION_HASH, AGENT, "git commit -m x", repo)
    if foreign:
        sessions.write_record(AGENT, {"root": OTHER, "model": "claude-haiku-5-5"}, repo)
    with pytest.raises(records.AttributionError) as info:
        attribution.committer_identity(
            CLAUDE_COMMIT_ENV, ["sh -c eval 'git commit -m x'"]
        )
    assert str(info.value) == (
        "The subagent's session record is missing or belongs to another root. "
        "Retry after the subagent's session record is restored."
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
    claude_hooks.handle_claude(data, CLAUDE_HOOK_ENV)
    sessions.write_record(AGENT, {"model": "claude-haiku-5-5"}, other)
    env = {**CLAUDE_COMMIT_ENV, "CLAUDE_PROJECT_DIR": str(other)}
    got = attribution.committer_identity(env, ["sh -c eval 'git commit -m x'"])
    assert got is not None
    assert got.ident.subagent == AGENT
    assert got.ident.model == "anthropic:claude-haiku-5-5"
    assert got.root_ident.model == "anthropic:claude-opus-5-5"
    with pytest.raises(records.AttributionError):
        attribution.committer_identity(env, ["unmatched"])
    assert storage.user_store().read_text() == "not a directory"
    assert not (repo / ".git" / storage.STORE).exists()
