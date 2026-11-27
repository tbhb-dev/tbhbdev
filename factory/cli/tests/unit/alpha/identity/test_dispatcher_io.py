# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity dispatcher."""

from __future__ import annotations

import os
import subprocess
import time
from typing import cast
lazy from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import (
    GOOD,
    HAIKU,
    MAIN,
    OPUS,
    SESSION_HASH,
    SONNET,
)
from unit.alpha.identity.identity_fixtures import (
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    CLAUDE_HOOK_ENV,
    CONV,
    HUMAN_COMMIT_ENV,
    NO_SESSION_MESSAGE,
    add_worktree,
    claude_data,
    claude_turn,
    claude_write,
    edit_by,
    feed,
    git_commit,
    hook_env,
    init_repo,
    last_message,
    loaded,
    msg_file,
    needs_prek,
    no_start,
    pending,
    reply,
    returning,
    run_git,
    stage,
    stage_doc,
    staged_text,
    trailers,
    write_transcript,
)

from tbhbdev.alpha.identity import dispatcher, records, sessions, storage
from tbhbdev.alpha.identity.commit import claims as commit_claims
from tbhbdev.alpha.identity.commit import hooks as commit_hooks
from tbhbdev.alpha.identity.commit import policy as commit_policy


@pytest.mark.parametrize("harness", ["claude", "codex", "antigravity"])
def test_record_outside_tbhb_dev_keeps_the_repository_untouched(
    repo: Path,
    harness: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_git(repo, "remote", "set-url", "origin", "git@github.com:someone/x.git")
    feed(monkeypatch, {"session_id": "s", "model": "m", "cwd": str(repo)})
    assert dispatcher.cmd_record(harness, {"CODEX_SESSION_ID": "s"}) == 0
    assert capsys.readouterr() == ("", "")
    assert not (repo / ".git" / "tbhb-alpha").exists()
    user_record = storage.user_store() / f"{records.hx(records.sha256_64('s'))}.json"
    if harness == "claude":
        assert loaded(user_record)["payload_model"] == "m"
    else:
        assert not user_record.exists()


def test_cmd_record_claude_dispatch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert dispatcher.cmd_record("nonsense", {}) == 0
    feed(monkeypatch, claude_data(repo))
    assert dispatcher.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    assert sessions.load_record(SESSION_HASH)["harness"] == "claude-code"
    assert len(pending(repo)) == 1
    # Without a session ID nothing is recorded.
    feed(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert dispatcher.cmd_record("claude", {}) == 0
    assert len(pending(repo)) == 1
    # session_id spellings are normalized.
    data = claude_data(repo)
    del data["session_id"]
    feed(monkeypatch, {**data, "sessionId": "session-raw"})
    assert dispatcher.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    assert len(pending(repo)) == 2


def test_cmd_record_session_from_env_and_filters(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"model": "<synthetic>", "effort": {"level": "high"}})
    assert (
        dispatcher.cmd_record("antigravity", {"ANTIGRAVITY_CONVERSATION_ID": CONV}) == 0
    )
    rec = sessions.load_record(records.sha256_64(CONV))
    assert no_start(rec) == {"harness": "antigravity", "effort": "high"}
    feed(monkeypatch, {"model": "gpt-5.6-sol", "session_id": CONV})
    assert dispatcher.cmd_record("antigravity", {}) == 0
    assert no_start(sessions.load_record(records.sha256_64(CONV))) == {
        "harness": "antigravity",
        "effort": "high",
        "model": "gpt-5.6-sol",
    }
    # No session anywhere, or no repository: nothing is written.
    feed(monkeypatch, {"model": "m"})
    assert dispatcher.cmd_record("antigravity", {}) == 0
    assert len(list((repo / ".git" / "tbhb-alpha" / "sessions").glob("*"))) == 1


def test_cmd_record_outside_a_repository(
    plain_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert dispatcher.cmd_record("antigravity", {}) == 0
    assert list(plain_checkout.iterdir()) == []


def test_cmd_record_claude_outside_a_repository_keeps_a_user_record(
    plain_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m", "cwd": str(plain_checkout)})
    assert dispatcher.cmd_record("claude", {}) == 0
    assert list(plain_checkout.iterdir()) == []
    assert sessions.load_record(records.sha256_64("s"))["payload_model"] == "m"


def test_cmd_record_uses_the_hook_cwd_outside_a_repository(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    feed(monkeypatch, {"session_id": "s", "model": "m", "cwd": str(outside)})
    assert dispatcher.cmd_record("claude", {}) == 0
    assert not (repo / ".git" / "tbhb-alpha" / "sessions").exists()
    assert (
        storage.user_store() / f"{records.hx(records.sha256_64('s'))}.json"
    ).is_file()


@pytest.mark.parametrize("event", ["SessionStart", "PostModelSwitch"])
def test_cmd_record_uses_the_hook_cwd_in_another_owners_repository(
    plain_checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
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
    assert dispatcher.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
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
    user_record = storage.user_store() / f"{records.hx(records.sha256_64('s'))}.json"
    assert no_start(loaded(user_record)) == expected
    # Missing values in later hooks must preserve a known model and switch.
    feed(monkeypatch, {"session_id": "s", "cwd": str(foreign)})
    assert dispatcher.cmd_record("claude", {}) == 0
    assert no_start(loaded(user_record)) == expected
    assert not (foreign / ".git" / "tbhb-alpha").exists()
    assert run_git(foreign, "status", "--porcelain") == status
    assert sorted((foreign / ".git").iterdir()) == entries
    assert list(plain_checkout.iterdir()) == []


def test_git_init_in_a_plain_directory_finishes_its_user_record(
    plain_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The directory is no repository before the command and another owner's
    # repository after it: the finish must still reach the user's store alone.
    data = {
        "session_id": "s",
        "cwd": str(plain_checkout),
        "tool_name": "Bash",
        "tool_input": {"command": "git init"},
    }
    feed(monkeypatch, {**data, "hook_event_name": "PreToolUse"})
    assert dispatcher.cmd_record("claude", {}) == 0
    subprocess.run(["git", "init", "-q", str(plain_checkout)], check=True)
    before = sorted((plain_checkout / ".git").iterdir())
    feed(monkeypatch, {**data, "hook_event_name": "PostToolUse"})
    assert dispatcher.cmd_record("claude", {}) == 0
    (path,) = (storage.user_store() / "cmds" / records.hx(records.sha256_64("s"))).glob(
        "*.json"
    )
    assert loaded(path)["done"] is not None
    assert sorted((plain_checkout / ".git").iterdir()) == before


@pytest.mark.parametrize("agent", [None, "child"])
@pytest.mark.parametrize("post", ["PostToolUse", "PostToolUseFailure"])
def test_git_init_in_a_plain_directory_finishes_each_agents_record(
    plain_checkout: Path, monkeypatch: pytest.MonkeyPatch, agent: str | None, post: str
) -> None:
    data = {
        "session_id": "s",
        "agent_id": agent,
        "cwd": str(plain_checkout),
        "tool_name": "Bash",
        "tool_input": {"command": "git init"},
    }
    feed(monkeypatch, {**data, "hook_event_name": "PreToolUse"})
    assert dispatcher.cmd_record("claude", {}) == 0
    subprocess.run(["git", "init", "-q", str(plain_checkout)], check=True)
    feed(monkeypatch, {**data, "hook_event_name": post})
    assert dispatcher.cmd_record("claude", {}) == 0
    (path,) = (storage.user_store() / "cmds" / records.hx(records.sha256_64("s"))).glob(
        "*.json"
    )
    rec = loaded(path)
    assert rec["done"] is not None
    assert rec["agent"] == (records.sha256_64(agent) if agent else None)


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
    assert dispatcher.cmd_record("claude", {}) == 0
    assert not (foreign / ".git" / "tbhb-alpha").exists()
    assert not (repo / ".git" / "tbhb-alpha").exists()
    if agent:
        assert not storage.user_store().exists()
    else:
        assert sorted(p.name for p in storage.user_store().rglob("*")) == [
            f"{records.hx(records.sha256_64('s'))}.json",
            "lock",
        ]


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


@pytest.mark.usefixtures("repo")
def test_main_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "human@example.invalid")
    path = msg_file(tmp_path)
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert dispatcher.main(["record", "codex"], {}) == 0
    assert dispatcher.main(["record", "antigravity", "extra"], {}) == 0
    assert dispatcher.main(["trailer", str(path)], {}) == 0
    assert dispatcher.main(["trailer", str(path), "message", "sha"], {}) == 0
    assert dispatcher.main(["trailer", str(path), "squash"], CLAUDE_COMMIT_ENV) == 0
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"
    assert dispatcher.main(["consume"], {}) == 0
    assert dispatcher.main(["consume", "x"], {}) == 0
    assert dispatcher.main(["check", str(path)], {}) == 0
    assert dispatcher.main(["check", str(path), "x"], {}) == 0
    assert capsys.readouterr().err == ""
    for argv in (
        cast("list[str]", []),
        ["record"],
        ["trailer"],
        ["check"],
        ["bogus"],
        ["bogus", "x"],
    ):
        assert dispatcher.main(argv, {}) == 2
        assert capsys.readouterr().err == dispatcher.USAGE + "\n"
    assert (
        dispatcher.USAGE == "usage: tbhbdev alpha identity record <harness> | "
        "trailer <msgfile> [source] | trailer --dry-run "
        "| consume | check <msgfile> | stamp-docs"
    )


def test_runs_as_tbhbdev_alpha_identity(tmp_path: Path) -> None:
    done = subprocess.run(
        ["tbhbdev", "alpha", "identity", "nope"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=hook_env(),
    )
    assert (done.returncode, done.stdout, done.stderr) == (
        2,
        "",
        dispatcher.USAGE + "\n",
    )
    done = subprocess.run(
        [
            "tbhbdev",
            "alpha",
            "identity",
            "check",
            str(msg_file(tmp_path, "s\n\nGenerated-by: x\n")),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=hook_env(),
    )
    assert done.returncode == 1


def test_run_reports_a_crash_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> int:
        raise KeyError(RAW_ID)

    monkeypatch.setattr(commit_claims, "cmd_consume", boom)
    assert dispatcher.run(["consume"], {}) == 1
    line = boom.__code__.co_firstlineno + 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha identity consume: failed: KeyError in boom (line {line})\n"
    )
    monkeypatch.setattr(commit_claims, "cmd_consume", returning(0))
    assert dispatcher.run(["consume"], {}) == 0


def test_run_lets_a_crashed_record_hook_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_harness: str, _env: object) -> int:
        raise KeyError(RAW_ID)

    monkeypatch.setattr(dispatcher, "cmd_record", boom)
    assert dispatcher.run(["record", "claude"], {}) == 0
    line = boom.__code__.co_firstlineno + 1
    assert capsys.readouterr() == (
        "",
        f"tbhbdev alpha identity record: failed: KeyError in boom (line {line})\n",
    )


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (["trailer", "MSG"], "cmd_trailer"),
        (["check", "MSG"], "cmd_check"),
        (["consume"], "cmd_consume"),
    ],
)
def test_run_fails_a_crashed_commit_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    name: str,
) -> None:
    def boom(*_: object) -> int:
        raise KeyError(RAW_ID)

    monkeypatch.setattr(
        commit_claims if name == "cmd_consume" else commit_hooks, name, boom
    )
    assert dispatcher.run(argv, {}) == 1
    assert capsys.readouterr().err.startswith(
        f"tbhbdev alpha identity {argv[0]}: failed: KeyError in boom"
    )


RAW_ID = "raw-session-id-0000"


@needs_prek
def test_install_writes_plain_identity_hooks(wired: Path) -> None:
    hooks = wired / ".git" / "hooks"
    for hook, command in (
        ("prepare-commit-msg", "trailer"),
        ("commit-msg", "check"),
        ("post-commit", "consume"),
    ):
        text = (hooks / hook).read_text(encoding="utf-8")
        assert "hook-impl" not in text
        assert f'tbhbdev alpha identity {command} "$@"' in text
        assert os.access(hooks / hook, os.X_OK)
        # The config has no hooks for these stages: nothing to chain.
        assert not (hooks / f"{hook}.prek").exists()
    assert "prek" in (hooks / "pre-commit").read_text(encoding="utf-8")


@needs_prek
def test_e2e_human_commit_is_untouched(wired: Path) -> None:
    stage(wired, "h.txt")
    done = git_commit(wired, "human work", HUMAN_COMMIT_ENV, "-s")
    assert done.returncode == 0, done.stderr
    assert run_git(wired, "log", "-1", "--format=%ae") == "h@example.invalid\n"
    assert "Signed-off-by: Human <h@example.invalid>" in last_message(wired)
    assert trailers(wired) == []


@needs_prek
def test_e2e_hook_works_from_a_subdirectory(wired: Path) -> None:
    claude_turn(wired)
    stage(wired, "sub/x.txt")
    done = git_commit(wired, "feat: x", CLAUDE_E2E, cwd=wired / "sub")
    assert done.returncode == 0, done.stderr
    assert len(trailers(wired)) == 1


@needs_prek
def test_e2e_malformed_trailer_is_rejected(wired: Path) -> None:
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a\n\nGenerated-by: robot")
    assert done.returncode != 0
    assert "must start with literal LLM" in done.stderr


@needs_prek
@pytest.mark.parametrize(
    ("env", "stamped"),
    [
        (CLAUDE_E2E | {commit_policy.RUN_MODEL_ENV: "claude-opus-5-5"}, True),
        (
            {
                f"GIT_{role}_{field}": value
                for role in ("AUTHOR", "COMMITTER")
                for field, value in (("NAME", "Human"), ("EMAIL", "h@example.invalid"))
            },
            False,
        ),
    ],
)
def test_e2e_merge_commits_get_trailers_only_from_an_agent(
    wired: Path, env: dict[str, str], *, stamped: bool
) -> None:
    run_git(wired, "checkout", "-q", "-b", "side")
    stage(wired, "side.txt")
    run_git(wired, "commit", "-q", "--no-verify", "-m", "side")
    run_git(wired, "checkout", "-q", "-")
    stage(wired, "main.txt")
    run_git(wired, "commit", "-q", "--no-verify", "-m", "main")
    merged = subprocess.run(
        ["git", "merge", "-q", "--no-commit", "--no-ff", "side"],
        capture_output=True,
        text=True,
        check=False,
        cwd=wired,
        env=hook_env(env),
    )
    assert merged.returncode == 0, merged.stderr
    done = git_commit(wired, "merge side", env)
    assert done.returncode == 0, done.stderr
    assert bool(trailers(wired)) is stamped


@needs_prek
def test_e2e_two_sessions_in_two_worktrees_commit_apart(
    wired: Path, tmp_path: Path
) -> None:
    """Each worktree keeps its own records, and its commit credits its own session."""
    trees = {
        raw: add_worktree(wired, tmp_path / raw)
        for raw in ("session-one", "session-two")
    }
    for raw, tree in trees.items():
        claude_write(tree, "same.txt", f"{raw}\n", session_id=raw)
    gitdirs = {raw: wired / ".git" / "worktrees" / raw for raw in trees}
    for raw, gitdir in gitdirs.items():
        store = gitdir / "tbhb-alpha"
        assert [p.stem for p in (store / "sessions").glob("*.json")] == [
            records.hx(records.sha256_64(raw))
        ]
        assert len(list((store / "edits").rglob("*.json"))) == 1
    assert not (wired / ".git" / "tbhb-alpha" / "edits").exists()
    for raw, tree in trees.items():
        run_git(tree, "add", "same.txt")
        done = git_commit(
            tree, "feat: x", {**CLAUDE_E2E, "CLAUDE_CODE_SESSION_ID": raw}
        )
        assert done.returncode == 0, done.stderr
        assert no_start(trailers(tree)) == [
            (
                "Generated-by: LLM harness=claude-code:2.1.316;"
                f"model=anthropic:claude-opus-5-5;session={records.sha256_64(raw)};"
                "surface=cli"
            )
        ]
        (receipt,) = (gitdirs[raw] / "tbhb-alpha" / "edits").rglob("*.json")
        assert loaded(receipt)["consumed"] is True


def test_cmd_record_writes_the_sessions_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert dispatcher.cmd_record("antigravity", {}) == 0
    assert sorted(os.listdir(repo / ".git" / "tbhb-alpha")) == ["lock", "sessions"]  # noqa: PTH208 -- exact on-disk names


@pytest.mark.usefixtures("repo")
def test_main_runs_each_command_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert dispatcher.main(["record", "antigravity"], {}) == 0
    assert sessions.load_record(records.sha256_64("s"))["harness"] == "antigravity"
    path = msg_file(tmp_path)
    env = CLAUDE_COMMIT_ENV | {
        commit_policy.RUN_HOOK_ENV: "1",
        commit_policy.RUN_MODEL_ENV: "claude-opus-5-5",
    }
    assert dispatcher.main(["trailer", str(path)], env) == 0
    assert "model=anthropic:claude-opus-5-5" in path.read_text(encoding="utf-8")


def test_main_dispatches_stamp_docs(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    edit_by(MAIN, "docs/a.md", 1_800_000_000)
    assert dispatcher.main(["stamp-docs", "ignored"], {}) == 0
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert "stamp-docs" in dispatcher.USAGE
