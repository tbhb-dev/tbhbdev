"""Tests for `tbhbdev alpha`'s dispatch to its commands."""

import io
import json
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from test_alpha_agent_settings import EXAMPLE

import tbhbdev.alpha.guard.agent_model as guard_agent_model
import tbhbdev.alpha.guard.gh_config_dir as guard_gh_config_dir
import tbhbdev.alpha.guard.gh_poll.command as guard_gh_poll
import tbhbdev.alpha.guard.gh_pr.command as guard_gh_pr
import tbhbdev.alpha.guard.op.command as guard_op
import tbhbdev.alpha.guard.ops_no_lazy_stop as guard_ops_no_lazy_stop
import tbhbdev.alpha.guard.scripts.command as guard_scripts
import tbhbdev.alpha.guard.sendmessage_clock as guard_sendmessage_clock
import tbhbdev.alpha.monorepo.command as monorepo
from tbhbdev import cli
from tbhbdev.alpha import (
    brief,
    close,
    gh_app_command,
    pre_push_guard,
    repo_bootstrap,
    review,
    run_stop_hook,
    verify_range,
    version_check,
)
from tbhbdev.alpha import command as alpha
from tbhbdev.alpha.agent import command as agent
from tbhbdev.alpha.agent import ssh_serve
from tbhbdev.alpha.agent.run_host import dispatcher
from tbhbdev.alpha.identity import dispatcher as identity_dispatcher
from tbhbdev.alpha.install import command as install
from tbhbdev.alpha.merge import command as squash_merge
from tbhbdev.alpha.pull_request import command as pr_command

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
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

    monkeypatch.setattr(identity_dispatcher, "run", run)
    monkeypatch.setattr(guard_gh_pr, "main", guard)
    monkeypatch.setattr(pr_command, "main", fake("pr", 5))
    monkeypatch.setattr(squash_merge, "main", fake("merge", 6))
    monkeypatch.setattr(install, "main", fake("install", 7))
    monkeypatch.setattr(close, "main", fake("close", 8))
    assert alpha.main(["identity", "check", "m"], "p") == 3
    assert alpha.main(["guard-gh-pr", "x"], "p") == 4
    assert alpha.main(["pr", "check"], "p") == 5
    assert alpha.main(["pr", "delete-branch", "3"], "p") == 5
    assert alpha.main(["merge", "3"], "p") == 6
    assert alpha.main(["install", "--dry-run"], "p") == 7
    assert alpha.main(["pr", "close", "3", "--reason", "r"], "p") == 8
    assert alpha.main(["pr", "close", "3", "--reason=r"], "p") == 8
    assert alpha.main(["issue", "close", "3"], "p") == 8
    assert calls == [
        ("identity", (["check", "m"], os.environ)),
        ("guard", None),
        ("pr", (["check"], "p pr")),
        ("pr", (["delete-branch", "3"], "p pr")),
        ("merge", (["3"], "p merge")),
        ("install", (["--dry-run"], "p install")),
        ("close", (["pr", "close", "3", "--reason", "r"], "p pr")),
        ("close", (["pr", "close", "3", "--reason=r"], "p pr")),
        ("close", (["issue", "close", "3"], "p issue")),
    ]


@pytest.mark.parametrize(
    ("name", "module", "args", "style"),
    [
        ("agent", agent, ["status", "sample"], "direct"),
        ("brief", brief, ["new", "demo"], "direct"),
        ("gh-app", gh_app_command, ["--help"], "positional"),
        ("guard-agent-model", guard_agent_model, [], "hook"),
        ("guard-gh-config-dir", guard_gh_config_dir, [], "hook"),
        ("guard-gh-poll", guard_gh_poll, [], "hook"),
        ("guard-op", guard_op, [], "hook"),
        ("guard-ops-no-lazy-stop", guard_ops_no_lazy_stop, [], "hook"),
        ("guard-scripts", guard_scripts, [], "hook"),
        ("guard-sendmessage-clock", guard_sendmessage_clock, [], "hook"),
        ("monorepo", monorepo, ["-h"], "positional"),
        ("pre-push-guard", pre_push_guard, ["origin", "url"], "direct"),
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


@pytest.mark.parametrize(
    ("verb", "module", "function", "args"),
    [
        ("run-host", dispatcher, "main", ["example", "--host", "example-linux"]),
        ("ssh-serve", ssh_serve, "main", ["extra"]),
    ],
)
def test_agent_host_command_wiring(
    verb: str,
    module: ModuleType,
    function: str,
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=37)
    monkeypatch.setattr(module, function, run)
    assert agent.main([verb, *args], "example agent") == 37
    run.assert_called_once_with(args, f"example agent {verb}")


@pytest.mark.parametrize(
    "run",
    [
        lambda: agent.main([], "example agent"),
        lambda: agent.main(["status", "sample"], "example agent"),
    ],
)
def test_command_refuses_missing_configuration(
    config_file: Path, capsys: pytest.CaptureFixture[str], run: Callable[[], int]
) -> None:
    assert run() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert str(config_file) in out.err
    assert "config cannot be read" in out.err


def test_command_reports_a_config_error(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text('[agent]\nsockets_dir = "/x"\nzeta = 1\n')
    assert agent.main(["status", "sample"], "example agent") == 1
    assert capsys.readouterr() == (
        "",
        f"example agent: {config_file}: [agent] zeta not recognized\n",
    )


@pytest.mark.parametrize("absolute", [False, True])
def test_config_check_refuses_write_paths_under_socket_parents(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    absolute: bool,
) -> None:
    home = config_file.parent.parent / "agent"
    monkeypatch.setenv("HOME", str(home))
    write = ".local/state/tbhbdev/agent-audit"
    configured = str(home / write) if absolute else write
    text = EXAMPLE.replace(
        'sockets_dir = "/example/sockets"',
        'sockets_dir = ".local/state/tbhbdev/sockets"',
    ).replace('audit_dir = ".local/state/example-audit"', f'audit_dir = "{configured}"')
    config_file.write_text(text)

    assert agent.main([], "example agent") == 1
    assert capsys.readouterr() == (
        "",
        (
            f"example agent: protected host path {home}/.local/state/tbhbdev "
            f"overlaps write root {home / write}; narrow that write root\n"
        ),
    )


@pytest.mark.parametrize("sockets", [".control/sockets", "/example/sockets"])
def test_config_check_accepts_unrelated_write_paths(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sockets: str,
) -> None:
    monkeypatch.setenv("HOME", str(config_file.parent.parent / "agent"))
    config_file.write_text(EXAMPLE.replace("/example/sockets", sockets))
    assert agent.main([], "example agent") == 0
    assert capsys.readouterr() == ("example agent: configuration valid\n", "")


def test_config_check_refuses_write_paths_under_the_shell_config_directory(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = config_file.parent.parent / "agent"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZDOTDIR", ".shell-config")
    config_file.write_text(
        EXAMPLE.replace(
            'audit_dir = ".local/state/example-audit"',
            'audit_dir = ".shell-config/audit"',
        )
    )

    assert agent.main([], "example agent") == 1
    assert capsys.readouterr() == (
        "",
        (
            f"example agent: protected host path {home}/.shell-config "
            f"overlaps write root {home}/.shell-config/audit; narrow that write root\n"
        ),
    )


@pytest.mark.parametrize(
    ("argv", "rc"), [(["--help"], 0), (["exec", "x"], 2), (["-x"], 2)]
)
def test_command_options(
    argv: list[str], capsys: pytest.CaptureFixture[str], rc: int
) -> None:
    assert agent.main(argv, "example agent") == rc
    out = capsys.readouterr()
    assert "usage: example agent" in out.out + out.err
    assert "run-host" not in out.out
