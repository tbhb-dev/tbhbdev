# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import asyncio
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lint.preflight import preflight


def test_checks_for_changed_paths() -> None:
    always = preflight.checks_for([], has_draft=False)
    expected = (
        "actionlint agent-capacity-images agent-capacity-modules "
        "agent-capacity-vulnerabilities design-report gazelle "
        "github-metrics-vulnerabilities gitleaks-history lint-baselines "
        "module-tidy pr-size prose"
    )
    assert set(always) == set(expected.split())
    paths = ["factory/cli/src/tbhbdev/alpha/command.py", "README.md"]
    assert preflight.checks_for(paths, has_draft=True) == (
        "affected-tests",
        "cli-coverage",
        "gitleaks-text",
        *always,
    )


def test_summary_for_mixed_results() -> None:
    assert preflight.summary([("gazelle", True), ("prose", False)], "origin/main") == (
        "PASS gazelle  bazelisk run //:preflight -- --check gazelle origin/main\n"
        "FAIL prose  bazelisk run //:preflight -- --check prose origin/main"
    )


def test_main_keeps_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_check(_name: str, _: preflight.Revisions, scratch: Path) -> None:
        (scratch / "design-report.md").write_text("report\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.setattr(sys, "argv", ["preflight", "--check", "design-report"])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(preflight, "git", AsyncMock(return_value=""))
    monkeypatch.setattr(preflight, "check", fake_check)

    assert asyncio.run(preflight.main()) == 0

    (report,) = tmp_path.glob("preflight-*/design-report.md")
    assert report.read_text(encoding="utf-8") == "report\n"
    assert str(report.parent) in capsys.readouterr().out


@pytest.mark.parametrize(
    "case",
    ["pass", "fail", "single", "dirty", "became-dirty", "moved", "unchanged"],
)
def test_main_records_full_runs_at_a_clean_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    head, tree, base = "a" * 40, "b" * 40, "c" * 40
    status = ("status", "--porcelain", "--untracked-files=no")
    replies: dict[tuple[str, ...], str] = {
        status: " M tracked" if case == "dirty" else "",
        ("rev-parse", "HEAD"): head,
        ("rev-parse", "--verify", "--end-of-options", "stack^{commit}"): base,
        ("merge-base", base, head): head if case == "unchanged" else base,
        (
            "rev-parse",
            "--verify",
            "--symbolic-full-name",
            "--end-of-options",
            "stack",
        ): "refs/heads/stack",
        ("diff", "--name-only", "-z", "--no-renames", base, head): "tracked\0",
        ("diff", "--name-only", "-z", "--no-renames", head, head): "",
        ("rev-parse", "--git-common-dir"): ".git",
        ("rev-parse", f"{head}^{{tree}}"): tree,
    }

    async def fake_git(*args: str) -> str:
        return replies[args]

    async def fake_check(name: str, _: preflight.Revisions, _scratch: Path) -> None:
        if case == "moved":
            replies["rev-parse", "HEAD"] = "d" * 40
        if case == "became-dirty":
            replies[status] = " M tracked"
        if case == "fail" and name in {"gazelle", "prose"}:
            raise preflight.CheckError(name)

    monkeypatch.setenv("BUILD_WORKSPACE_DIRECTORY", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    opts = ["--check", "gazelle"] if case == "single" else []
    monkeypatch.setattr(sys, "argv", ["preflight", "stack", *opts])
    monkeypatch.setattr(preflight, "git", fake_git)
    monkeypatch.setattr(preflight, "check", fake_check)
    (tmp_path / ".git").mkdir()
    record = tmp_path / ".git" / "tbhbdev-preflight" / head

    if case == "dirty":
        with pytest.raises(SystemExit) as error:
            asyncio.run(preflight.main())
        assert error.value.code == 2
    else:
        assert asyncio.run(preflight.main()) == (case == "fail")

    if case == "pass":
        assert record.read_text() == f"tree {tree}\nbase {base}\nresult pass\n"
    elif case == "fail":
        assert (
            record.read_text()
            == f"tree {tree}\nbase {base}\nresult fail gazelle prose\n"
        )
    else:
        assert not record.parent.exists()


def test_affected_tests_excludes_slow_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = tmp_path / "targets.txt"
    targets.write_text(
        "//ops/ansible:lint_test\n//ops/ansible:syntax_test\n", encoding="utf-8"
    )
    (tmp_path / "selection").write_text(f"file={targets}\n", encoding="utf-8")
    commands: list[tuple[object, ...]] = []

    async def fake_run(*command: object, **_kwargs: object) -> bytes:
        commands.append(command)
        return b""

    monkeypatch.setattr(preflight, "run", fake_run)

    asyncio.run(preflight.affected_tests("base-sha", "head-sha", tmp_path))

    assert commands[-1] == (
        "bazelisk",
        "test",
        "--config=ci",
        "--test_tag_filters=-slow-external",
        "--build_tests_only",
        f"--target_pattern_file={targets}",
    )


def _configure_single_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_name: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.setattr(sys, "argv", ["preflight", "--check", check_name])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(preflight, "git", AsyncMock(return_value=""))


def _outcome_error(outcome: str) -> BaseException | None:
    errors: dict[str, BaseException] = {
        "fail": preflight.CheckError(outcome),
        "timeout": TimeoutError(),
        "cancel": asyncio.CancelledError(),
        "interrupt": KeyboardInterrupt(),
    }
    return errors.get(outcome)


@pytest.mark.parametrize("outcome", ["pass", "fail", "timeout", "cancel", "interrupt"])
def test_pr_size_shuts_down_bazel_before_worktree_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    revisions = preflight.Revisions("base-sha", "merge-sha", "head-sha", "origin/main")
    events: list[tuple[tuple[object, ...], object, object]] = []
    failure = _outcome_error(outcome)

    async def fake_run(
        *command: object, cwd: object = None, seconds: object = None, **_kwargs: object
    ) -> bytes:
        events.append((command, cwd, seconds))
        if failure is not None and "pr-size" in command:
            raise failure
        return b""

    monkeypatch.setattr(preflight, "git", AsyncMock(return_value="agent/test"))
    monkeypatch.setattr(preflight, "run", fake_run)

    if failure is None:
        asyncio.run(preflight.check("pr-size", revisions, tmp_path))
    else:
        with pytest.raises(type(failure)):
            asyncio.run(preflight.check("pr-size", revisions, tmp_path))

    base_checkout = tmp_path / "base"
    shutdown = (("bazelisk", "shutdown"), base_checkout, 30)
    remove = (("git", "worktree", "remove", "--force", base_checkout), None, None)
    assert shutdown in events
    assert remove in events
    assert events.index(shutdown) < events.index(remove)


@pytest.mark.parametrize(
    ("check_outcome", "shutdown_outcome"),
    [
        ("pass", "fail"),
        ("pass", "timeout"),
        ("fail", "fail"),
        ("timeout", "timeout"),
    ],
)
def test_pr_size_shutdown_failure_keeps_result_and_removes_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    check_outcome: str,
    shutdown_outcome: str,
) -> None:
    revisions = preflight.Revisions("base-sha", "merge-sha", "head-sha", "origin/main")
    commands: list[tuple[object, ...]] = []
    check_error = _outcome_error(check_outcome)
    shutdown_error = _outcome_error(shutdown_outcome)

    async def fake_run(*command: object, **_kwargs: object) -> bytes:
        commands.append(command)
        if check_error is not None and "pr-size" in command:
            raise check_error
        if shutdown_error is not None and command == ("bazelisk", "shutdown"):
            raise shutdown_error
        return b""

    monkeypatch.setattr(preflight, "git", AsyncMock(return_value="agent/test"))
    monkeypatch.setattr(preflight, "run", fake_run)

    if check_error is None:
        asyncio.run(preflight.check("pr-size", revisions, tmp_path))
    else:
        with pytest.raises(type(check_error)):
            asyncio.run(preflight.check("pr-size", revisions, tmp_path))

    assert ("git", "worktree", "remove", "--force", tmp_path / "base") in commands
    assert "base" in capsys.readouterr().err


def _is_select_command(command: tuple[object, ...]) -> bool:
    return (
        command[:1] == ("bash",) and len(command) > 1 and "select.sh" in str(command[1])
    )


def _write_empty_selection(env: dict[str, str] | None) -> None:
    assert env is not None
    targets = Path(env["RUNNER_TEMP"]) / "targets.txt"
    targets.write_text("", encoding="utf-8")
    Path(env["GITHUB_OUTPUT"]).write_text(f"file={targets}\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("check_outcome", "shutdown_outcome"),
    [
        ("pass", "pass"),
        ("pass", "fail"),
        ("pass", "timeout"),
        ("fail", "fail"),
    ],
)
def test_affected_tests_shuts_down_bazel_before_scratch_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    check_outcome: str,
    shutdown_outcome: str,
) -> None:
    _configure_single_check(tmp_path, monkeypatch, "affected-tests")
    events: list[tuple[str, tuple[object, ...], object, object]] = []
    check_error = _outcome_error(check_outcome)
    shutdown_error = _outcome_error(shutdown_outcome)
    real_rmtree = shutil.rmtree

    def tracked_rmtree(path: Path) -> None:
        events.append(("rmtree", (), Path(path), None))
        real_rmtree(path)

    async def fake_run(
        *command: object,
        cwd: object = None,
        seconds: object = None,
        env: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> bytes:
        events.append(("run", command, cwd, seconds))
        if _is_select_command(command):
            if check_error is not None:
                raise check_error
            _write_empty_selection(env)
        if shutdown_error is not None and command == ("bazelisk", "shutdown"):
            raise shutdown_error
        return b""

    monkeypatch.setattr(preflight, "run", fake_run)
    monkeypatch.setattr(shutil, "rmtree", tracked_rmtree)

    expected_exit = 0 if check_error is None else 1
    assert asyncio.run(preflight.main()) == expected_exit

    shutdown_entries = [
        (index, cwd, seconds)
        for index, (kind, command, cwd, seconds) in enumerate(events)
        if kind == "run" and command == ("bazelisk", "shutdown")
    ]
    assert len(shutdown_entries) == 1
    shutdown_index, shutdown_cwd, shutdown_seconds = shutdown_entries[0]
    assert isinstance(shutdown_cwd, Path)
    assert shutdown_cwd.name == "checkout"
    assert shutdown_seconds == 30
    scratch = shutdown_cwd.parent

    if check_error is None:
        rmtree_index = events.index(("rmtree", (), scratch, None))
        assert shutdown_index < rmtree_index
        assert not scratch.exists()
    if shutdown_error is not None:
        assert str(shutdown_cwd) in capsys.readouterr().err


@pytest.mark.parametrize("outcome", ["fail", "timeout", "cancel", "interrupt"])
def test_affected_tests_shuts_down_bazel_on_error_and_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    events: list[tuple[tuple[object, ...], object, object]] = []
    failure = _outcome_error(outcome)
    assert failure is not None

    async def fake_run(
        *command: object, cwd: object = None, seconds: object = None, **_kwargs: object
    ) -> bytes:
        events.append((command, cwd, seconds))
        if _is_select_command(command):
            raise failure
        return b""

    monkeypatch.setattr(preflight, "run", fake_run)

    with pytest.raises(type(failure)):
        asyncio.run(preflight.affected_tests("base-sha", "head-sha", tmp_path))

    assert (("bazelisk", "shutdown"), tmp_path / "checkout", 30) in events


@pytest.mark.parametrize("check_name", ["affected-tests", "pr-size"])
def test_sigterm_runs_shutdown_before_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_name: str
) -> None:
    _configure_single_check(tmp_path, monkeypatch, check_name)
    events: list[tuple[tuple[object, ...], object]] = []

    async def fake_run(
        *command: object, cwd: object = None, **_kwargs: object
    ) -> bytes:
        events.append((command, cwd))
        is_target = (
            _is_select_command(command)
            if check_name == "affected-tests"
            else "pr-size" in command
        )
        if is_target:
            loop = asyncio.get_running_loop()
            loop.call_later(0.02, os.kill, os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)
        return b""

    monkeypatch.setattr(preflight, "run", fake_run)
    previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(preflight.main())
    finally:
        signal.signal(signal.SIGTERM, previous)

    expected_dir = "checkout" if check_name == "affected-tests" else "base"
    shutdown_matches = [
        (index, cwd)
        for index, (command, cwd) in enumerate(events)
        if command == ("bazelisk", "shutdown")
    ]
    assert len(shutdown_matches) == 1
    shutdown_index, shutdown_cwd = shutdown_matches[0]
    assert isinstance(shutdown_cwd, Path)
    assert shutdown_cwd.name == expected_dir
    if check_name == "pr-size":
        remove_index = events.index(
            (("git", "worktree", "remove", "--force", shutdown_cwd), None)
        )
        assert shutdown_index < remove_index
