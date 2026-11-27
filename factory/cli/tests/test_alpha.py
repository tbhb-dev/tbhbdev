"""Tests for `tbhbdev alpha`'s dispatch to its commands."""

import io
import json
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev import cli
from tbhbdev.alpha import (
    agent,
    brief,
    close,
    gh_app,
    guard_agent_model,
    guard_gh_config_dir,
    guard_gh_pr,
    guard_op,
    guard_scripts,
    guard_ssh_agent,
    identity,
    install,
    loop,
    monorepo,
    pr_stats,
    pull_request,
    repo_bootstrap,
    review,
    run_stop_hook,
    squash_merge,
    verify_range,
    version_check,
)
from tbhbdev.alpha import command as alpha

if TYPE_CHECKING:
    from types import ModuleType

USAGE = alpha.USAGE.format(prog="tbhbdev alpha")


def test_alpha_is_a_tool() -> None:
    assert cli.TOOLS["alpha"] is alpha.main


def test_usage_says_the_commands_are_interim() -> None:
    assert "interim: they may change or go away without notice" in USAGE
    for name in ("identity", "guard-gh-pr", "pr", "issue", "merge", "install"):
        assert f"\n  {name} " in USAGE


def test_no_arguments_prints_usage_to_stderr_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert alpha.main([], "tbhbdev alpha") == 2
    assert capsys.readouterr() == ("", USAGE)


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_to_stdout(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert alpha.main([flag, "ignored"], "tbhbdev alpha") == 0
    assert capsys.readouterr() == (USAGE, "")


def test_unknown_command_fails_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert alpha.main(["nope", "x"], "tbhbdev alpha") == 2
    assert capsys.readouterr() == (
        "",
        f"tbhbdev alpha: unknown command 'nope'\n{USAGE}",
    )


def test_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    def run(argv: list[str], env: object) -> int:
        calls.append(("identity", (argv, env)))
        return 3

    def guard() -> int:
        calls.append(("guard", None))
        return 4

    def fake(name: str, code: int) -> object:
        def main(argv: list[str], prog: str) -> int:
            calls.append((name, (argv, prog)))
            return code

        return main

    monkeypatch.setattr(identity, "run", run)
    monkeypatch.setattr(guard_gh_pr, "main", guard)
    monkeypatch.setattr(pull_request, "main", fake("pr", 5))
    monkeypatch.setattr(pr_stats, "main", fake("pr-stats", 9))
    monkeypatch.setattr(squash_merge, "main", fake("merge", 6))
    monkeypatch.setattr(install, "main", fake("install", 7))
    monkeypatch.setattr(close, "main", fake("close", 8))
    assert alpha.main(["identity", "check", "m"], "p") == 3
    assert alpha.main(["guard-gh-pr", "x"], "p") == 4
    assert alpha.main(["pr", "check"], "p") == 5
    assert alpha.main(["pr", "delete-branch", "3"], "p") == 5
    assert alpha.main(["pr-stats", "o/r"], "p") == 9
    assert alpha.main(["merge", "3"], "p") == 6
    assert alpha.main(["install", "--dry-run"], "p") == 7
    assert alpha.main(["pr", "close", "3", "--reason", "r"], "p") == 8
    assert alpha.main(["pr", "close", "3", "--reason=r"], "p") == 8
    assert alpha.main(["pr", "close", "3", "--comment", "c"], "p") == 8
    assert alpha.main(["issue", "close", "3"], "p") == 8
    assert calls == [
        ("identity", (["check", "m"], os.environ)),
        ("guard", None),
        ("pr", (["check"], "p pr")),
        ("pr", (["delete-branch", "3"], "p pr")),
        ("pr-stats", (["o/r"], "p pr-stats")),
        ("merge", (["3"], "p merge")),
        ("install", (["--dry-run"], "p install")),
        ("close", (["pr", "close", "3", "--reason", "r"], "p pr")),
        ("close", (["pr", "close", "3", "--reason=r"], "p pr")),
        ("close", (["pr", "close", "3", "--comment", "c"], "p pr")),
        ("close", (["issue", "close", "3"], "p issue")),
    ]


@pytest.mark.parametrize(
    ("name", "module", "args", "style"),
    [
        ("agent", agent, ["status", "sample"], "direct"),
        ("brief", brief, ["new", "demo"], "direct"),
        ("gh-app", gh_app, ["--help"], "positional"),
        ("guard-agent-model", guard_agent_model, [], "hook"),
        ("guard-gh-config-dir", guard_gh_config_dir, [], "hook"),
        ("guard-op", guard_op, [], "hook"),
        ("guard-scripts", guard_scripts, [], "hook"),
        ("guard-ssh-agent", guard_ssh_agent, [], "hook"),
        ("loop", loop, [], "direct"),
        ("monorepo", monorepo, ["-h"], "positional"),
        ("repo", repo_bootstrap, ["bootstrap", "o/r"], "keyword"),
        ("review", review, ["check", "7"], "keyword"),
        ("run-stop-hook", run_stop_hook, ["claude"], "direct"),
        ("verify-range", verify_range, ["base", "tip"], "direct"),
        ("version-check", version_check, ["--json"], "direct"),
    ],
)
def test_command_wiring(
    name: str,
    module: ModuleType,
    args: list[str],
    style: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert f"\n  {name} " in USAGE
    run = Mock(return_value=7)
    if style == "direct":
        assert alpha.COMMANDS[name] is vars(module)["main"]
        monkeypatch.setitem(alpha.COMMANDS, name, run)
    else:
        monkeypatch.setattr(module, "main", run)
    assert alpha.main([name, *args], "p") == 7
    prog = f"p {name}"
    if style == "hook":
        run.assert_called_once_with()
    elif style == "keyword":
        run.assert_called_once_with(args, prog=prog)
    else:
        run.assert_called_once_with(args, prog)


def test_guard_runs_from_the_top(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({})))
    assert cli.main(["alpha", "guard-gh-pr"]) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("command", ["state-guard", "session-context"])
def test_harness_hooks_run_from_the_top_without_config(
    command: str,
    config_file: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert config_file is not None
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({})))
    assert cli.main(["alpha", command]) == 0
    assert capsys.readouterr() == ("", "")
