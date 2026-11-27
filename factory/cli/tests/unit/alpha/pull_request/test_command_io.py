# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request command."""

import json
from subprocess import CompletedProcess
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from fake_tool import PR_ORIGIN
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_draft_samples import replace_once

from tbhbdev.alpha.pull_request import command as pr_command
from tbhbdev.alpha.pull_request import draft as pr_draft
from tbhbdev.alpha.pull_request import draft_io as pr_draft_io
from tbhbdev.alpha.pull_request import publish as pr_publish
from tbhbdev.alpha.pull_request import sizing as pr_sizing
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.draft import DRAFT
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

import socket
from typing import TYPE_CHECKING

from tbhbdev import userconfig

if TYPE_CHECKING:
    from pathlib import Path

import os
import subprocess
import sys

from fake_tool import PR_ORIGIN as ORIGIN
from fake_tool import (
    pythonpath,
)
from unit.alpha.pull_request.pr_draft_samples import BASE
from unit.alpha.pull_request.pr_test_support import (
    OPENER,
    POSTING,
    SRC,
    World,
    ledgers,
    run_main,
    write_draft,
)

from tbhbdev.alpha.identity.commit.policy import AGENT_EMAIL

agent = pr_test_support.agent
draft_world = pr_test_support.draft_world
host_names = pr_test_support.host_names
world = pr_test_support.world


def test_check_prints_ok(
    draft_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    write_draft(draft_world)
    assert run_main("check") == 0
    assert capsys.readouterr().out == (
        "0 code lines, limit 400\n0 raw lines, limit 1500\nOK: the draft conforms\n"
    )


def test_check_prints_problems_with_paths(
    draft_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_draft(draft_world, replace_once(BASE, "\nw\n", "\nw <!-- c -->\n"))
    assert run_main("check", str(path)) == 1
    assert capsys.readouterr().out == (
        f"{path}:5: HTML comment left in the description\n"
        "0 code lines, limit 400\n0 raw lines, limit 1500\n"
    )


def test_check_defaults_to_the_draft_path(
    draft_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    del draft_world
    assert run_main("check") == 1
    out = capsys.readouterr().out
    assert out == (
        ".github/PULL_REQUEST_DRAFT.md:1: "
        "cannot read the draft: No such file or directory\n"
        "0 code lines, limit 400\n0 raw lines, limit 1500\n"
    )


def test_create_runs_gh_with_the_frontmatter(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    meta = (
        "---\nbase: dev\ndraft: true\nlabels: [bug, good first issue]\n"
        "reviewers: [tbhb, org/team]\nassignees: ['@me']\nmilestone: v1\n---\n"
    )
    world.git("update-ref", "refs/remotes/origin/dev", "HEAD")
    write_draft(world, meta + BASE)
    assert run_main("create") == 0
    _, call = world.calls("gh")
    assert call["argv"] == [
        "pr", "create", "--title", "fix: x", "--body-file", "-",
        "--base", "dev", "--draft",
        "--label", "bug", "--label", "good first issue",
        "--reviewer", "tbhb", "--reviewer", "org/team",
        "--assignee", "@me", "--milestone", "v1",
    ]  # fmt: skip
    assert (
        call["stdin"]
        == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    assert call["stdin_tty"] is False
    assert capsys.readouterr().out == "https://github.com/tbhb-dev/tbhbdev/pull/1\n"


def test_create_defaults(world: World) -> None:
    write_draft(world)
    assert run_main("create") == 0
    _, call = world.calls("gh")
    assert call["argv"] == [
        "pr",
        "create",
        "--title",
        "fix: x",
        "--body-file",
        "-",
        "--base",
        "main",
    ]


@pytest.mark.parametrize("context", ["session", "account"])
def test_agent_create_refuses_when_hooks_are_disabled(
    world: World,
    agent: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    context: str,
) -> None:
    write_draft(world)
    world.git("config", "core.hooksPath", "/dev/null")
    env = agent if context == "session" else {}
    if context == "account":
        world.git("config", "user.email", AGENT_EMAIL)
    assert run_main("create", env=env) == 1
    assert world.calls("gh") == []
    assert "/dev/null/" in capsys.readouterr().err


@pytest.mark.parametrize("ending", ["", "\n", "\n\n"])
def test_create_passes_gh_errors_through(
    world: World, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    message = "aborted: you must first push the current branch to a remote" + ending
    world.fake("gh", rc=1, stderr=message)
    write_draft(world)
    assert run_main("create") == 1
    captured = capsys.readouterr()
    assert captured.err == message.rstrip("\n") + "\n"
    assert captured.out == ""
    assert len(world.calls("gh")) == 2
    assert world.git("status", "--porcelain", "--ignored").count("push") == 0


def test_create_stops_on_problems(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    write_draft(world, "## Why\n\nw\n")
    assert run_main("create") == 1
    assert world.calls("gh") == []
    assert ":1: the first line" in capsys.readouterr().out


def test_dry_run_prints_argv_and_body(
    world: World, capsys: pytest.CaptureFixture[str], agent: dict[str, str]
) -> None:
    write_draft(world, "---\nlabels: [bug]\n---\n" + BASE)
    assert run_main("create", "--dry-run", env=agent) == 0
    out = capsys.readouterr().out
    assert out == (
        "gh pr create --title 'fix: x' --body-file - --base main --label bug\n\n"
        "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n\n"
        f"{OPENER}\nAssisted-by: {POSTING}\n"
    )
    assert not ledgers(world).exists()


def test_edit(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    write_draft(
        world, "---\nlabels: [bug, docs]\ndraft: true\nbase: main\n---\n" + BASE
    )
    assert run_main("edit", str(DRAFT), "--pr", "7") == 0
    _, _, call = world.calls("gh")
    assert call["argv"] == [
        "pr", "edit", "7", "--title", "fix: x", "--body-file", "-",
        "--add-label", "bug", "--add-label", "docs",
    ]  # fmt: skip
    assert (
        call["stdin"]
        == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    assert capsys.readouterr().out == "https://github.com/tbhb-dev/tbhbdev/pull/1\n"


def test_edit_without_labels_and_with_agent(
    world: World, agent: dict[str, str]
) -> None:
    write_draft(world)
    assert run_main("edit", str(DRAFT), "--pr", "7", env=agent) == 0
    _, _, call = world.calls("gh")
    assert call["argv"] == ["pr", "edit", "7", "--title", "fix: x", "--body-file", "-"]
    assert str(call["stdin"]).endswith(f"v\n\nAssisted-by: {POSTING}\n")


def test_edit_requires_a_draft(world: World) -> None:
    write_draft(world)
    with pytest.raises(SystemExit) as info:
        run_main("edit", "--pr", "7")
    assert info.value.code == 2


def test_edit_requires_a_pr_number(world: World) -> None:
    del world
    with pytest.raises(SystemExit) as info:
        run_main("edit", str(DRAFT))
    assert info.value.code == 2


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as info:
        run_main()
    assert info.value.code == 2


def test_main_defaults_to_the_process_environment(
    world: World, monkeypatch: pytest.MonkeyPatch, agent: dict[str, str]
) -> None:
    write_draft(world)
    for key, val in agent.items():
        monkeypatch.setenv(key, val)
    assert pr_command.main(["create", "--dry-run"]) == 0
    assert pr_command.main(["check"]) == 0


def test_runs_as_tbhbdev_alpha_pr(world: World) -> None:
    write_draft(world)
    (world.bin / "bazelisk").unlink()
    world.fake("bash", rc=1, stderr="missing counter")
    bare = world.root.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    world.git("push", "-q", str(bare), "HEAD:refs/heads/main")
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath(SRC),
        # The fetch of the base branch reaches this local repository.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{bare}.insteadOf",
        "GIT_CONFIG_VALUE_0": ORIGIN,
    }
    env.pop("MUTANT_UNDER_TEST", None)
    done = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "alpha", "pr", "check"],
        cwd=world.root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert (done.returncode, done.stderr) == (
        1,
        "size count unavailable: missing counter\n",
    )


def test_help_describes_the_script(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run_main("--help")
    assert "\n\nCheck a PR draft and open the PR.\n\n" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["edit"])
def test_pr_number_must_be_an_integer(command: str) -> None:
    with pytest.raises(SystemExit) as info:
        run_main(command, str(DRAFT), "--pr", "seven")
    assert info.value.code == 2


def test_pr_help_lists_rebase_and_close(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main("--help")
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "ready,rebase,rerun" in out
    assert "close N --reason TEXT: close a pull request" in out


def test_help_uses_the_program_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        pr_command.main(["--help"], {}, prog="tbhbdev alpha pr")
    assert capsys.readouterr().out.startswith("usage: tbhbdev alpha pr ")


def test_help_lists_close(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        pr_command.main(["--help"], {}, prog="tbhbdev alpha pr")
    assert "close N --reason TEXT: close a pull request" in capsys.readouterr().out


@pytest.mark.parametrize(
    "case",
    [
        ((0, "agent/prepare-EXAMPLEHOST01"), b"", "branch name contains a host name"),
        ((0, ""), b"", "cannot read the current branch"),
        ((128, ""), b"", "cannot read the current branch"),
        (
            (0, "agent/topic"),
            b'[agent]\nhost = "invalid"\n',
            "cannot read known host names",
        ),
        ((0, "agent/topic"), b"\xff", "cannot read known host names"),
    ],
)
def test_create_checks_the_branch_for_host_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[tuple[int, str], bytes, str],
) -> None:
    git_result, config_bytes, message = case
    monkeypatch.setattr(socket, "gethostname", lambda: "examplehost01.example.test")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = userconfig.path()
    config.parent.mkdir()
    config.write_bytes(config_bytes)
    monkeypatch.setattr(pr_command, "require_git_hooks", Mock(return_value=True))
    monkeypatch.setattr(pr_tools, "find_tools", Mock(return_value={}))
    git = Mock(return_value=git_result)
    monkeypatch.setattr(pr_tools, "git", git)
    read_checked = Mock()
    monkeypatch.setattr(pr_draft_io, "read_checked", read_checked)

    assert pr_command.main(["create"], env={}) == 1
    assert capsys.readouterr().err == f"tbhbdev alpha pr: {message}\n"
    git.assert_called_once_with("branch", "--show-current")
    read_checked.assert_not_called()


@pytest.mark.usefixtures("isolated_home", "pr_checkout")
@pytest.mark.parametrize("verb", ["create", "edit"])
def test_pr_keeps_earliest_start(verb: str, monkeypatch: pytest.MonkeyPatch) -> None:
    early = "LLM harness=codex;model=openai:m;started_at=2026-11-14T00:02:41Z"
    late = early.replace("00:02:41", "00:09:00")
    other = late.replace("openai:m", "openai:other")
    messages = [
        f"one\n\nGenerated-by: {late}\nAssisted-by: {other}",
        f"two\n\nAssisted-by: {early}",
    ]
    if verb == "create":
        for message in messages:
            assert git("commit", "--allow-empty", "-qm", message)[0] == 0

    remote = PR_ORIGIN.removeprefix("git@").removesuffix(".git").replace(":", "/")
    pages = [[{"commit": {"message": message}}] for message in messages]
    actual_run = pr_tools.run
    posted = Mock()

    def run(
        argv: Sequence[str], *, cwd: Path | None = None, stdin: str = ""
    ) -> CompletedProcess[str]:
        if argv[0] != "gh":
            return actual_run(argv, cwd=cwd, stdin=stdin)
        match list(argv[1:3]):
            case ["pr", "view"] if verb == "create":
                return CompletedProcess(argv, 1, "", "No Pull Requests Found")
            case ["pr", "view"]:
                return CompletedProcess(
                    argv, 0, json.dumps({"url": f"https://{remote}/pull/40"}), ""
                )
            case ["api", _]:
                return CompletedProcess(argv, 0, json.dumps(pages), "")
            case ["pr", action]:
                posted(action, stdin)
            case _:
                pass
        return CompletedProcess(argv, 0, "{}", "")

    draft = pr_draft.Checked(
        [], "fix: x", ["# fix: x", "", "Example."], {"base": "main"}
    )
    monkeypatch.setattr(pr_draft_io, "read_checked", Mock(return_value=draft))
    monkeypatch.setattr(pr_sizing, "check_size", Mock())
    monkeypatch.setattr(pr_publish, "post_text", Mock())
    monkeypatch.setattr(pr_tools, "find_tools", Mock(return_value={}))
    monkeypatch.setattr(pr_tools, "find_tool", str)
    monkeypatch.setattr(pr_tools, "run", run)
    args = ["create"] if verb == "create" else ["edit", str(DRAFT), "--pr", "40"]
    assert pr_command.main(args, env={}) == 0
    posted.assert_called_once_with(
        verb, f"Example.\n\nGenerated-by: {early}\nAssisted-by: {other}\n"
    )
