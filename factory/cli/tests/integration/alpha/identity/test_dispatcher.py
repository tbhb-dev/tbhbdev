# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity dispatcher tests."""

import os
import subprocess
from typing import TYPE_CHECKING, cast

import pytest
from integration.alpha.identity.identity_test_support import (
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    MAIN,
    claude_write,
    edit_by,
    feed,
    generated_subagents,
    git_commit,
    hook_env,
    msg_file,
    needs_prek,
    returning,
    run_git,
    stage_doc,
    staged_text,
)

from tbhbdev.alpha.identity import (
    claude_commands,
    dispatcher,
    sessions,
    validation,
    values,
)

if TYPE_CHECKING:
    from pathlib import Path


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
    env_source = {**CLAUDE_COMMIT_ENV, dispatcher.SOURCE_ENV: "squash"}
    assert dispatcher.main(["trailer", str(path)], env_source) == 0
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

    monkeypatch.setattr(dispatcher, "cmd_consume", boom)
    assert dispatcher.run(["consume"], {}) == 1
    line = boom.__code__.co_firstlineno + 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha identity consume: failed: KeyError in boom (line {line})\n"
    )
    monkeypatch.setattr(dispatcher, "cmd_consume", returning(0))
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

    monkeypatch.setattr(dispatcher, name, boom)
    assert dispatcher.run(argv, {}) == 1
    assert capsys.readouterr().err.startswith(
        f"tbhbdev alpha identity {argv[0]}: failed: KeyError in boom"
    )


def test_run_passes_the_environment() -> None:
    # A human merge commit's source, from the environment, makes trailer a no-op.
    assert dispatcher.run(["trailer", "MSG"], {dispatcher.SOURCE_ENV: "merge"}) == 0


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
def test_e2e_prek_commit_msg_hook_still_rejects_a_message(
    with_msg_linter: Path,
) -> None:
    """Regression: the plain commit-msg hook stopped running prek's hooks."""
    repo = with_msg_linter
    assert (repo / ".git" / "hooks" / "commit-msg.prek").is_file()
    claude_write(repo, "a.txt")
    run_git(repo, "add", "a.txt")
    head = run_git(repo, "rev-parse", "HEAD")
    done = git_commit(repo, "not conventional", CLAUDE_E2E)
    assert done.returncode != 0
    assert "cog: not a conventional commit" in done.stdout + done.stderr
    assert run_git(repo, "rev-parse", "HEAD") == head


@needs_prek
def test_e2e_prek_commit_msg_hook_passes_and_trailers_land(
    with_msg_linter: Path,
) -> None:
    repo = with_msg_linter
    claude_write(repo, "a.txt")
    run_git(repo, "add", "a.txt")
    done = git_commit(repo, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(repo) == [None]
    assert (repo / "cog.log").read_text(encoding="utf-8") == "ran\n"


@pytest.mark.usefixtures("repo")
def test_main_runs_each_command_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert dispatcher.main(["record", "antigravity"], {}) == 0
    assert sessions.load_record(values.sha256_64("s"))["harness"] == "antigravity"
    path = msg_file(tmp_path)
    env = CLAUDE_COMMIT_ENV | {
        values.RUN_HOOK_ENV: "1",
        values.RUN_MODEL_ENV: "claude-opus-5-5",
    }
    assert dispatcher.main(["trailer", str(path)], env) == 0
    assert "model=anthropic:claude-opus-5-5" in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("repo")
def test_run_trailer_passes_a_merge_commit(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    env = CLAUDE_COMMIT_ENV | {values.RUN_HOOK_ENV: "1"}
    assert dispatcher.trailer_command(str(path), "merge", env) == 0
    assert "Generated-by: LLM " in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("repo")
def test_run_trailer_refuses_a_successful_noop(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    env = CLAUDE_COMMIT_ENV | {values.RUN_HOOK_ENV: "1"}
    with pytest.raises(
        claude_commands.AttributionError,
        match=r"^run commit requires a Generated-by trailer$",
    ):
        dispatcher.trailer_command(str(path), "squash", env)
    with pytest.raises(claude_commands.AttributionError) as caught:
        dispatcher.trailer_command(str(path), None, {values.RUN_HOOK_ENV: "1"})
    assert str(caught.value) == validation.SESSION_ERROR


def test_run_trailer_preserves_a_hook_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher, "cmd_trailer", returning(3))
    env = CLAUDE_COMMIT_ENV | {values.RUN_HOOK_ENV: "1"}
    assert dispatcher.trailer_command(str(tmp_path / "absent"), "merge", env) == 3
    assert (
        dispatcher.trailer_command("--dry-run", None, {values.RUN_HOOK_ENV: "1"}) == 0
    )


def test_main_dispatches_stamp_docs(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    edit_by(MAIN, "docs/a.md", 1_800_000_000)
    assert dispatcher.main(["stamp-docs", "ignored"], {}) == 0
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert "stamp-docs" in dispatcher.USAGE
