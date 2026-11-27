# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for attribution in the prepare-commit-msg hook."""

import os
import subprocess
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from integration.alpha.identity.identity_test_support import (
    ASSIST_OTHER,
    CHILD,
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    CONV,
    EARLY,
    LAUNCH,
    OTHER,
    ROOT_THREAD,
    SESSION_HASH,
    SESSION_RAW,
    SUB_E2E,
    add_worktree,
    agy_post,
    claude_turn,
    claude_write,
    commit,
    generated_subagents,
    git_commit,
    hook_env,
    last_message,
    line_of,
    loaded,
    make_ident,
    make_receipt,
    meta,
    msg_file,
    needs_prek,
    no_start,
    patch_call,
    put_receipt,
    record,
    returning,
    run_git,
    stage,
    stdin_fixture,
    trailers,
    turn,
    write_rollout,
    write_transcript,
)

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import (
    claude_commands,
    commit_trailers,
    replay,
    sessions,
    storage,
    validation,
    values,
)
from tbhbdev.alpha.identity import commit as identity_commit
from tbhbdev.alpha.identity import trailers as identity_trailers

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.usefixtures("repo")
def test_cmd_trailer_skips_squashes_and_human_merges(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), "squash", CLAUDE_COMMIT_ENV) == 0
    assert identity_commit.cmd_trailer(str(path), "merge", {}) == 0
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert "Generated-by" in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("repo")
def test_cmd_trailer_stamps_an_agent_merge(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), "merge", CLAUDE_COMMIT_ENV) == 0
    assert "Generated-by: LLM " in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("repo", "plain")
def test_cmd_trailer_does_nothing_outside_harness_or_repo(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), None, {}) == 0
    assert (
        identity_commit.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    )  # cwd is `plain`
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"


def test_cmd_trailer_scans_codex_rollouts(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_VERSION": "1",
    }
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, cwd=str(repo)),
            turn("gpt-5.6-sol", "high"),
            patch_call("a.txt"),
        ],
    )
    stage(repo, "a.txt")
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), None, env) == 0
    session = values.sha256_64(ROOT_THREAD)
    assert path.read_text(encoding="utf-8") == (
        "subject\n\nbody\n\n"
        "Generated-by: LLM effort=high;harness=codex:1;"
        f"model=openai:gpt-5.6-sol;session={session}\n"
    )
    assert len(list((repo / ".git" / "tbhb-alpha" / "marks").glob("*.json"))) == 1
    # Claude does not scan rollouts.
    other = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(other), None, CLAUDE_COMMIT_ENV) == 0


def test_cmd_trailer_keeps_the_fallback_without_any_generated_by(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo, f"s\n\n{ASSIST_OTHER[0]}: {ASSIST_OTHER[1]}\n")
    monkeypatch.setattr(replay, "ancestry", returning(["git commit --amend -m x"]))
    path = msg_file(tmp_path, "x\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    keys = [k for k, _ in validation.llm_trailers(path.read_text(encoding="utf-8"))]
    assert keys == ["Generated-by", "Assisted-by"]


def test_cmd_trailer_merges_a_model_less_receipt_into_its_twin(
    repo: Path, tmp_path: Path
) -> None:
    stage(repo, "a.txt")
    sessions.write_record(
        SESSION_HASH, {"model": "claude-opus-5-5", "effort": "low"}, repo
    )
    full = make_ident(
        model="anthropic:claude-opus-5-5", root=SESSION_HASH, effort="low"
    )
    bare = make_ident(model=None, root=SESSION_HASH)
    put_receipt(repo, "a1.json", make_receipt("a.txt", full, ts=2), SESSION_HASH)
    put_receipt(repo, "a2.json", make_receipt("a.txt", bare, ts=1), SESSION_HASH)
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    text = path.read_text(encoding="utf-8")
    assert text == (
        "subject\n\nbody\n\n"
        "Generated-by: LLM effort=low;harness=claude-code:2.1.316;"
        f"model=anthropic:claude-opus-5-5;session={SESSION_HASH};surface=cli\n"
    )
    assert validation.check_message(text) == []


@needs_prek
def test_e2e_claude_edit_then_commit(wired: Path) -> None:
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: add a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    session = values.sha256_64("session-raw")
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


@needs_prek
def test_e2e_hook_works_from_a_subdirectory(wired: Path) -> None:
    claude_turn(wired)
    stage(wired, "sub/x.txt")
    done = git_commit(wired, "feat: x", CLAUDE_E2E, cwd=wired / "sub")
    assert done.returncode == 0, done.stderr
    assert len(trailers(wired)) == 1


@needs_prek
@pytest.mark.parametrize(
    ("env", "stamped"),
    [
        (CLAUDE_E2E | {values.RUN_MODEL_ENV: "claude-opus-5-5"}, True),
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
    agent = values.sha256_64("agent-raw")
    done = git_commit(
        wired, "feat: a", {**CLAUDE_E2E, claude_commands.AGENT_ENV: agent}
    )
    assert done.returncode == 0, done.stderr
    session = values.sha256_64("session-raw")
    gen, assist = trailers(wired)
    assert gen.startswith("Generated-by: LLM ")
    assert f"session={session}" in gen
    assert "subagent" not in gen
    assert assist.startswith("Assisted-by: LLM ")
    assert f"subagent={agent}" in assist
    assert validation.check_message(last_message(wired)) == []


@needs_prek
def test_e2e_agy_edit_then_commit(wired: Path) -> None:
    (wired / "hello.txt").write_text("hi\n", encoding="utf-8")
    record(
        wired,
        "antigravity",
        agy_post(workspacePaths=[str(wired)]),
        {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"},
    )
    run_git(wired, "add", "hello.txt")
    env = {"ANTIGRAVITY_CONVERSATION_ID": CONV, "ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    done = git_commit(wired, "feat: hello", env)
    assert done.returncode == 0, done.stderr
    assert no_start(trailers(wired)) == [
        (
            "Generated-by: LLM effort=low;harness=antigravity:1.4.12;"
            f"model=gcp.gemini:gemini-3.8-flash;session={values.sha256_64(CONV)};"
            "surface=cli"
        )
    ]


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
            values.hx(values.sha256_64(raw))
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
                f"model=anthropic:claude-opus-5-5;session={values.sha256_64(raw)};"
                "surface=cli"
            )
        ]
        (receipt,) = (gitdirs[raw] / "tbhb-alpha" / "edits").rglob("*.json")
        assert loaded(receipt)["consumed"] is True


@needs_prek
@pytest.mark.parametrize("hook", ["prepare-commit-msg", "commit-msg"])
def test_e2e_a_concurrent_edit_during_a_hook_does_not_fail_the_commit(
    wired: Path, hook: str
) -> None:
    """Regression: under prek these stages failed on a concurrent edit.

    prek reported "files were modified by this hook" and stashed unstaged
    edits away while the hook ran.
    """
    (wired / "base.txt").write_text("unstaged\n", encoding="utf-8")
    shim = wired / ".git" / "hooks" / hook
    lines = shim.read_text(encoding="utf-8").splitlines(keepends=True)
    # Another agent's edit lands while the hook runs, and sees the real file.
    lines.insert(1, "cat base.txt > seen.txt; echo concurrent >> base.txt\n")
    shim.write_text("".join(lines), encoding="utf-8")
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert (wired / "seen.txt").read_text(encoding="utf-8") == "unstaged\n"
    base = (wired / "base.txt").read_text(encoding="utf-8")
    assert base == "unstaged\nconcurrent\n"
    assert generated_subagents(wired) == [None]


@needs_prek
def test_e2e_a_formatter_rewrite_keeps_the_subagent_trailer(wired: Path) -> None:
    config = wired / ".pre-commit-config.yaml"
    loaded_config = cast("dict[str, list[object]]", yaml.safe_load(config.read_text()))
    loaded_config["repos"].append(
        {"repo": "builtin", "hooks": [{"id": "trailing-whitespace"}]}
    )
    config.write_text(yaml.safe_dump(loaded_config), encoding="utf-8")
    run_git(wired, "add", ".pre-commit-config.yaml")
    claude_turn(wired)
    claude_write(wired, "b.txt", "sub   \n", agent_id="agent-raw")
    run_git(wired, "add", "b.txt")
    failed = git_commit(wired, "feat: b", CLAUDE_E2E)
    assert failed.returncode != 0
    assert (wired / "b.txt").read_text(encoding="utf-8") == "sub\n"
    run_git(wired, "add", "b.txt")
    done = git_commit(wired, "feat: b", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [SUB_E2E]
    (receipt,) = (wired / ".git" / "tbhb-alpha" / "edits").rglob("*.json")
    assert loaded(receipt)["consumed"] is True


@needs_prek
def test_e2e_an_edit_after_staging_is_credited_to_the_commit_that_has_it(
    wired: Path,
) -> None:
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    claude_write(wired, "a.txt", "a.txt\nsub\n", agent_id="agent-raw")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [None]
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: sub", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [SUB_E2E]


def test_cmd_trailer_ranks_by_blob_from_a_subdirectory(
    repo: Path, tmp_path: Path
) -> None:
    stage(repo, "top.txt")
    blob = run_git(repo, "ls-files", "-s", "top.txt").split()[1]
    last = make_ident(root=SESSION_HASH, model="anthropic:last")
    stale = make_ident(CHILD, "anthropic:stale", SESSION_HASH)
    info = storage.repo_info()
    assert info is not None
    for name, who, receipt_blob, ts in (
        ("last", last, blob, 1),
        ("stale", stale, "other", 2),
    ):
        storage.write_json(
            info.state / "edits" / values.hx(SESSION_HASH) / f"{name}.json",
            make_receipt("top.txt", who, receipt_blob, ts),
        )
    (repo / "sub").mkdir()
    os.chdir(repo / "sub")
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    generated = [ln for ln in lines if ln.startswith("Generated-by:")]
    assert "model=anthropic:last" in generated[0]
    assert "model=anthropic:stale" in generated[1]


@pytest.mark.parametrize("operation", ["rm", "mv", "mode", "empty"])
def test_cmd_trailer_no_receipt_commit_cannot_suppress_committer(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    stage(repo, "a.txt")
    commit(repo)
    if operation == "rm":
        run_git(repo, "rm", "a.txt")
    elif operation == "mv":
        run_git(repo, "mv", "a.txt", "b.txt")
    elif operation == "mode":
        run_git(repo, "update-index", "--chmod=+x", "a.txt")
    monkeypatch.setattr(replay, "ancestry", returning(["git commit -m x"]))
    forged = line_of(EARLY, f"launch={LAUNCH};", who=OTHER)
    path = msg_file(tmp_path, f"subject\n\nGenerated-by: {forged}\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    found = validation.llm_trailers(path.read_text())
    assert any(
        commit_trailers.trailer_agent(value) == (SESSION_RAW, None)
        for _, value in found
    )


REFLOG_START = "2026-11-14T00:02:41Z"
SESSION_START = "2026-11-14T00:09:00Z"


@pytest.mark.usefixtures("isolated_home", "pr_checkout")
@pytest.mark.parametrize("branch", ["topic", "initial", "detached"])
def test_session_commit_start_from_branch_reflog_or_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    if branch == "topic":
        with monkeypatch.context() as dated:
            dated.setenv("GIT_COMMITTER_DATE", REFLOG_START)
            assert git_repo.git("switch", "-qc", "topic")[0] == 0
        assert git_repo.git("commit", "--allow-empty", "-qm", "later")[0] == 0
    elif branch == "detached":
        assert git_repo.git("switch", "--detach", "HEAD")[0] == 0
    env = {"CLAUDE_CODE_SESSION_ID": "session-raw"}
    sessions.write_record(
        values.sha256_64("session-raw"),
        {"model": "anthropic:m", "started_at": SESSION_START},
    )
    path = tmp_path / "MSG"
    path.write_text("subject\n")
    assert identity_commit.cmd_trailer(str(path), "message", env) == 0
    [(_, trailer)] = validation.llm_trailers(path.read_text())
    assert identity_trailers.trailer_start(trailer) == (
        REFLOG_START if branch == "topic" else SESSION_START
    )
