# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity claude hooks."""

from __future__ import annotations

import json
import os
import time
lazy from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import (
    HAIKU,
    OPUS,
    SESSION_HASH,
    SONNET,
    VERSIONED_MAIN,
    Json,
)
from unit.alpha.identity.identity_fixtures import (
    AGENT,
    CLAUDE_E2E,
    CLAUDE_HOOK_ENV,
    claude_data,
    claude_write,
    git_commit,
    loaded,
    needs_prek,
    no_start,
    pending,
    receipts_of,
    run_git,
    stage,
    stdin_fixture,
    switch_data,
    trailers,
    write_transcript,
)

from tbhbdev.alpha.identity import models, records, sessions
from tbhbdev.alpha.identity import trailers as identity_trailers
from tbhbdev.alpha.identity.claude import commands as claude_commands
from tbhbdev.alpha.identity.claude import hooks as claude_hooks
from tbhbdev.alpha.identity.commands import cmd_hashes


def test_record_child_stores_hashes_only(repo: Path) -> None:
    transcript = write_transcript(
        repo / "session-raw.jsonl", {"message": {"model": "claude-opus-5-5"}}
    )
    child = write_transcript(
        repo / "session-raw" / "subagents" / "agent-agent-raw.jsonl",
        {"message": {"model": "claude-haiku-5-5"}},
    )
    data = stdin_fixture(
        "claude-pretooluse-bash-child.json",
        transcript_path=str(transcript),
        cwd=str(repo),
    )
    agent = claude_hooks.record_child(data, "high")
    assert agent == records.sha256_64("agent-raw")
    record = sessions.load_record(agent)
    assert no_start(record) == {
        "harness": "claude-code",
        "root": records.sha256_64("session-raw"),
        "model": "claude-haiku-5-5",
        "effort": "high",
        "transcript": str(child),
    }
    # File names and ids are hashes. (The transcript path is a filesystem path
    # the harness chose, and kept as is.)
    names = [p.name for p in (repo / ".git" / "tbhb-alpha").rglob("*.json")]
    assert names == [f"{records.hx(agent)}.json"]


EVENT_FAIL = "PostToolUseFailure"


def test_claude_ident_main_and_child(repo: Path) -> None:
    main = claude_data(repo)
    assert claude_hooks.claude_ident(main, CLAUDE_HOOK_ENV) == VERSIONED_MAIN
    write_transcript(
        repo / "session-raw" / "subagents" / "agent-agent-raw.jsonl",
        {"message": {"model": "claude-haiku-5-5"}},
    )
    child = claude_data(repo, "claude-pretooluse-bash-child.json")
    assert claude_hooks.claude_ident(child, CLAUDE_HOOK_ENV) == models.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        AGENT,
        "anthropic:claude-haiku-5-5",
        "high",
        "cli",
    )


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        ({"model": "claude-sonnet-5-5"}, "anthropic:claude-sonnet-5-5"),
        ({"payload_model": "claude-haiku-5-5"}, "anthropic:claude-haiku-5-5"),
        ({"version": "9.9.9"}, None),
    ],
    ids=["record-model", "payload-model", "none-anywhere"],
)
def test_claude_ident_main_falls_back_to_the_record_model(
    repo: Path, recorded: Json, expected: str | None
) -> None:
    data = claude_data(repo)
    # A transcript tail with no assistant entry, such as after a large tool result.
    (repo / "session-raw.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "x"}}) + "\n",
        encoding="utf-8",
    )
    sessions.write_record(SESSION_HASH, recorded, repo)
    assert claude_hooks.claude_ident(data, CLAUDE_HOOK_ENV).model == expected


def test_claude_ident_main_prefers_the_transcript_model(repo: Path) -> None:
    sessions.write_record(SESSION_HASH, {"model": "claude-sonnet-5-5"}, repo)
    got = claude_hooks.claude_ident(claude_data(repo), CLAUDE_HOOK_ENV)
    assert got.model == "anthropic:claude-opus-5-5"


def test_claude_ident_child_never_takes_the_main_record_model(repo: Path) -> None:
    sessions.write_record(SESSION_HASH, {"model": "claude-sonnet-5-5"}, repo)
    child = claude_data(repo, "claude-pretooluse-bash-child.json")
    assert claude_hooks.claude_ident(child, CLAUDE_HOOK_ENV).model is None


def test_claude_ident_falls_back_to_the_recorded_version(repo: Path) -> None:
    sessions.write_record(SESSION_HASH, {"version": "9.9.9"}, repo)
    got = claude_hooks.claude_ident(claude_data(repo), {})
    assert got.harness == "claude-code:9.9.9"
    assert got.surface is None
    assert claude_hooks.claude_ident(claude_data(repo), CLAUDE_HOOK_ENV).harness == (
        "claude-code:2.1.316"
    )
    other = claude_data(repo, session_id="other")
    assert claude_hooks.claude_ident(other, {}).harness == "claude-code"


def test_main_agent_events_record_the_session_and_commands(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claude_hooks.handle_claude(claude_data(repo), CLAUDE_HOOK_ENV)
    assert no_start(sessions.load_record(SESSION_HASH)) == {
        "harness": "claude-code",
        "version": "2.1.316",
        "model": "claude-opus-5-5",
        "effort": "high",
        "transcript": str(repo / "session-raw.jsonl"),
    }
    (rec,) = pending(repo)
    assert (rec["agent"], rec["done"]) == (None, None)
    assert rec["cmd"] == cmd_hashes("cd x && git commit -m m")
    claude_hooks.handle_claude(
        claude_data(repo, hook_event_name=EVENT_FAIL), CLAUDE_HOOK_ENV
    )
    assert pending(repo) == [{**rec, "done": pending(repo)[0]["done"]}]
    assert isinstance(pending(repo)[0]["done"], float)
    assert receipts_of(repo, SESSION_HASH) == []
    assert capsys.readouterr().out == ""


def test_subagent_events_record_the_child_and_its_commands(repo: Path) -> None:
    data = claude_data(repo, "claude-pretooluse-bash-child.json")
    claude_hooks.handle_claude(data, CLAUDE_HOOK_ENV)
    assert sessions.load_record(AGENT)["root"] == SESSION_HASH
    assert sessions.load_record(SESSION_HASH) == {}
    (rec,) = pending(repo)
    assert rec["agent"] == AGENT


def test_bash_edit_diffs_become_receipts(repo: Path) -> None:
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    response = {"bashEditDiff": "diff --git a/x.txt b/x.txt\n+++ b/x.txt\n"}
    post = claude_data(repo, hook_event_name="PostToolUse", tool_response=response)
    claude_hooks.handle_claude(post, CLAUDE_HOOK_ENV)
    (got,) = receipts_of(repo, SESSION_HASH)
    assert (got["path"], got["subagent"], got["harness"]) == (
        "x.txt",
        None,
        "claude-code:2.1.316",
    )
    # Failures and pre events never write receipts; a response without paths neither.
    for event in ("PreToolUse", "PostToolUseFailure"):
        claude_hooks.handle_claude({**post, "hook_event_name": event}, CLAUDE_HOOK_ENV)
    claude_hooks.handle_claude({**post, "tool_response": {"x": 1}}, CLAUDE_HOOK_ENV)
    assert len(receipts_of(repo, SESSION_HASH)) == 1


REAL_BASH_EDIT_DIFF = {
    "files": [
        {
            "filePath": "x.txt",
            "hunks": [
                {
                    "oldStart": 1,
                    "oldLines": 1,
                    "newStart": 1,
                    "newLines": 1,
                    "lines": ["-a", "+b"],
                }
            ],
        }
    ],
    "moreFiles": 0,
    "shared": True,
}


@pytest.mark.parametrize(
    "command",
    [
        "git checkout other",
        "git switch -q main && git status -sb | head -2",
        "git pull --ff-only -q && git log -1 && git status --short | head",
        "git -C sub reset --hard HEAD 2>&1",
        "git -c core.x=1 stash pop >/dev/null",
        "git stash pop\ngrep -n a x.txt",
        "cd sub && git pull",
        "FOO=1 BAR=2 git checkout other",
        "git pull 2>&1 | tail -3",
        "git checkout main & git status",
    ],
)
def test_git_work_tree_moves_write_no_receipt(repo: Path, command: str) -> None:
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    base = claude_data(repo)
    pre = {**base, "tool_input": {"command": command}}
    claude_hooks.handle_claude(pre, CLAUDE_HOOK_ENV)
    post = {
        **pre,
        "hook_event_name": "PostToolUse",
        "tool_response": {"bashEditDiff": REAL_BASH_EDIT_DIFF},
    }
    claude_hooks.handle_claude(post, CLAUDE_HOOK_ENV)
    assert receipts_of(repo, SESSION_HASH) == []


@pytest.mark.parametrize(
    "command",
    [
        "sed -i s/a/b/ x.txt",
        "ruff format x.txt",
        "printf b > x.txt",
        "git checkout other && sed -i s/a/b/ x.txt",
        "git checkout other && ruff format x.txt",
        "git checkout other > x.txt",
        "git checkout main & sed -i s/a/b/ x.txt",
        "git checkout $(sed -i s/a/b/ x.txt; echo main)",
        "git checkout `sed -i s/a/b/ x.txt`",
        "git checkout main <(sed -i s/a/b/ x.txt)",
        "git rebase -x 'sed -i s/a/b/ x.txt' main",
        "git rebase --exec 'ruff format x.txt' main",
        "FOO=1 sed -i s/a/b/ x.txt",
        "git status",
        "git",
        "git -C",
        "echo 'unclosed",
        "",
    ],
)
def test_other_bash_writes_still_get_a_receipt(repo: Path, command: str) -> None:
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    base = claude_data(repo)
    post = {
        **base,
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": command},
        "tool_response": {"bashEditDiff": REAL_BASH_EDIT_DIFF},
    }
    claude_hooks.handle_claude(post, CLAUDE_HOOK_ENV)
    assert [r["path"] for r in receipts_of(repo, SESSION_HASH)] == ["x.txt"]


def test_edit_tools_become_receipts(repo: Path) -> None:
    for name in ("a.txt", "b.ipynb", "c.txt"):
        (repo / name).write_text("x\n", encoding="utf-8")
    write = stdin_fixture("claude-posttooluse-write.json")
    base = {
        **write,
        "cwd": str(repo),
        "transcript_path": str(repo / "session-raw.jsonl"),
    }
    (repo / "session-raw.jsonl").write_text("", encoding="utf-8")
    claude_hooks.handle_claude(
        {**base, "tool_input": {"file_path": "a.txt"}}, CLAUDE_HOOK_ENV
    )
    claude_hooks.handle_claude(
        {
            **base,
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "b.ipynb"},
        },
        CLAUDE_HOOK_ENV,
    )
    claude_hooks.handle_claude(
        {**base, "tool_name": "MultiEdit", "tool_input": {"file_path": "c.txt"}}, {}
    )
    # Not edits: wrong tool, wrong event, no path.
    claude_hooks.handle_claude(
        {**base, "tool_name": "Read", "tool_input": {"file_path": "r"}}, {}
    )
    claude_hooks.handle_claude(
        {**base, "hook_event_name": "PreToolUse", "tool_input": {"file_path": "p"}}, {}
    )
    claude_hooks.handle_claude({**base, "tool_input": {}}, {})
    claude_hooks.handle_claude(
        {**base, "hook_event_name": "Stop", "tool_name": "Bash"}, {}
    )
    assert [r["path"] for r in receipts_of(repo, SESSION_HASH)] == [
        "a.txt",
        "b.ipynb",
        "c.txt",
    ]
    first = receipts_of(repo, SESSION_HASH)[0]
    assert first["harness"] == "claude-code:2.1.316"
    assert first["surface"] == "cli"


@needs_prek
def test_e2e_claude_edit_then_commit(wired: Path) -> None:
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: add a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    session = records.sha256_64("session-raw")
    (line,) = trailers(wired)
    assert (
        no_start(line) == "Generated-by: LLM harness=claude-code:2.1.316;"
        f"model=anthropic:claude-opus-5-5;session={session};surface=cli"
    )
    assert identity_trailers.start_ok(identity_trailers.trailer_start(line)) is not None
    receipts = list((wired / ".git" / "tbhb-alpha" / "edits").rglob("*.json"))
    assert [loaded(p)["consumed"] for p in receipts] == [True]
    # Consumed: a later commit of another file falls back to the committer.
    stage(wired, "b.txt")
    assert git_commit(wired, "chore: b", CLAUDE_E2E).returncode == 0
    assert len(trailers(wired)) == 1
    assert "harness=claude-code:2.1.316" in trailers(wired)[0]


def test_claude_events_follow_the_hook_cwd_not_the_process_cwd(
    repo: Path, plain_checkout: Path
) -> None:
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    child = claude_data(repo, "claude-pretooluse-bash-child.json")
    claude_hooks.handle_claude(child, CLAUDE_HOOK_ENV)
    assert sessions.load_record(AGENT, repo)["effort"] == "high"
    assert sessions.load_record(AGENT, repo)["root"] == SESSION_HASH
    assert sessions.load_record(SESSION_HASH, repo) == {}
    cmds = repo / ".git" / "tbhb-alpha" / "cmds"
    assert os.listdir(cmds) == [records.hx(SESSION_HASH)]  # noqa: PTH208 -- exact on-disk names
    (rec,) = pending(repo)
    assert (rec["agent"], rec["done"]) == (AGENT, None)
    done = {**child, "hook_event_name": "PostToolUse"}
    claude_hooks.handle_claude(done, CLAUDE_HOOK_ENV)
    assert isinstance(pending(repo)[0]["done"], float)
    assert plain_checkout.is_dir()


# PostToolUse tool_response for a Bash call moved to the background, as
# Claude Code 2.1.318 sends it: with run_in_background, and past its timeout.
MOVED_TO_BACKGROUND = (
    {
        "stdout": "",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
        "backgroundTaskId": "b1234567",
    },
    {
        "stdout": "",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
        "backgroundTaskId": "b7654321",
        "timedOutAfterMs": 3000,
    },
)


@pytest.mark.parametrize("response", MOVED_TO_BACKGROUND)
def test_a_command_moved_to_the_background_stays_pending(
    repo: Path, response: Json
) -> None:
    child = claude_data(repo, "claude-pretooluse-bash-child.json")
    claude_hooks.handle_claude(child, CLAUDE_HOOK_ENV)
    post = {**child, "hook_event_name": "PostToolUse", "tool_response": response}
    claude_hooks.handle_claude(post, CLAUDE_HOOK_ENV)
    assert [rec["done"] for rec in pending(repo)] == [None]
    line = "sh -c eval 'cd x && git commit -m m'"
    assert claude_commands.match_command_agent(SESSION_HASH, [line], repo) == AGENT
    finished = {**post, "tool_response": {**response, "backgroundTaskId": ""}}
    claude_hooks.handle_claude(finished, CLAUDE_HOOK_ENV)
    assert isinstance(pending(repo)[0]["done"], float)


def test_claude_main_events_follow_the_hook_cwd(
    repo: Path, plain_checkout: Path
) -> None:
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    main = claude_data(repo, effort="high")
    claude_hooks.handle_claude(main, CLAUDE_HOOK_ENV)
    assert sessions.load_record(SESSION_HASH, repo)["harness"] == "claude-code"
    response = {"bashEditDiff": "diff --git a/x.txt b/x.txt\n+++ b/x.txt\n"}
    post = {**main, "hook_event_name": "PostToolUse", "tool_response": response}
    claude_hooks.handle_claude(post, CLAUDE_HOOK_ENV)
    write = stdin_fixture(
        "claude-posttooluse-write.json", cwd=str(repo), transcript_path=""
    )
    claude_hooks.handle_claude(write, CLAUDE_HOOK_ENV)
    paths = sorted(str(r["path"]) for r in receipts_of(repo, SESSION_HASH))
    assert paths == ["hello.txt", "x.txt"]
    assert plain_checkout.is_dir()


def test_claude_ident_reads_the_record_in_the_hook_cwd(
    repo: Path, plain_checkout: Path
) -> None:
    sessions.write_record(SESSION_HASH, {"version": "9.9"}, repo)
    got = claude_hooks.claude_ident(claude_data(repo), {})
    assert got.harness == "claude-code:9.9"
    assert plain_checkout.is_dir()


def test_a_model_switch_is_recorded_with_its_time(repo: Path) -> None:
    before = time.time()
    claude_hooks.handle_claude(switch_data(repo), CLAUDE_HOOK_ENV)
    after = time.time()
    rec = sessions.load_record(SESSION_HASH)
    assert rec["switch_model"] == SONNET
    assert isinstance(rec["switch_at"], float)
    assert before <= rec["switch_at"] <= after
    assert rec["model"] == OPUS
    # A later hook reads the stale transcript, and leaves the switch alone.
    claude_hooks.handle_claude(
        claude_data(repo, "claude-posttooluse-write.json"), CLAUDE_HOOK_ENV
    )
    assert sessions.load_record(SESSION_HASH)["switch_model"] == SONNET
    assert sessions.load_record(SESSION_HASH)["switch_at"] == rec["switch_at"]
    assert pending(repo) == []


@pytest.mark.parametrize(
    ("event", "to_model"),
    [("PreModelSwitch", SONNET), ("PostModelSwitch", "<synthetic>")],
)
def test_only_a_real_post_switch_is_recorded(
    repo: Path, event: str, to_model: str
) -> None:
    data = {**switch_data(repo, to_model), "hook_event_name": event}
    claude_hooks.handle_claude(data, CLAUDE_HOOK_ENV)
    rec = sessions.load_record(SESSION_HASH)
    assert "switch_model" not in rec
    assert "switch_at" not in rec
    assert "payload_model" not in rec


def test_a_payload_model_is_recorded_apart_from_a_switch(repo: Path) -> None:
    start = claude_data(
        repo, hook_event_name="SessionStart", tool_name=None, model=HAIKU
    )
    claude_hooks.handle_claude(start, CLAUDE_HOOK_ENV)
    rec = sessions.load_record(SESSION_HASH)
    assert rec["payload_model"] == HAIKU
    assert "switch_model" not in rec
    named = claude_data(repo, hook_event_name="SessionStart", modelName=SONNET)
    claude_hooks.handle_claude(named, CLAUDE_HOOK_ENV)
    assert sessions.load_record(SESSION_HASH)["payload_model"] == SONNET
    fake = claude_data(repo, hook_event_name="SessionStart", model="<synthetic>")
    claude_hooks.handle_claude(fake, CLAUDE_HOOK_ENV)
    assert sessions.load_record(SESSION_HASH)["payload_model"] == SONNET
