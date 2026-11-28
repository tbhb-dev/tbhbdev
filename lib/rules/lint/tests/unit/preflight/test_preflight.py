# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import asyncio
import sys
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from lint.preflight import preflight

if TYPE_CHECKING:
    from pathlib import Path


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


CLI_TEST = "//factory/cli/tests/unit:test_command"


def drive_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    selection: str | None,
) -> tuple[list[str], list[float]]:
    """Run main over an ops-only change; return the checks run and the clock."""
    ran: list[str] = []
    now = [100.0]
    seconds = {"affected-tests": 7.0, "cli-coverage": 40.0}

    async def fake_git(*args: str) -> str:
        return "ops/templates/a.j2\0" if args[0] == "diff" else ""

    async def fake_check(name: str, _: preflight.Revisions, scratch: Path) -> None:
        ran.append(name)
        now[0] += seconds.get(name, 1.0)
        if name == "affected-tests" and selection is not None:
            targets = scratch / "targets.txt"
            targets.write_text(selection, encoding="utf-8")
            all_targets = "true" if "//..." in selection else "false"
            (scratch / "selection").write_text(
                f"all={all_targets}\nfile={targets}\n", encoding="utf-8"
            )
        if name == "affected-tests" and selection == "unreadable":
            (scratch / "selection").write_text("file=/nonexistent\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.setattr(sys, "argv", ["preflight", *argv])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(preflight, "git", fake_git)
    monkeypatch.setattr(preflight, "check", fake_check)
    monkeypatch.setattr(preflight, "clock", lambda: now[0])
    assert asyncio.run(preflight.main()) == 0
    return ran, now


def test_ops_only_selection_skips_cli_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran, _ = drive_main(tmp_path, monkeypatch, [], "//ops/tests:test_a\n")

    captured = capsys.readouterr()
    assert "cli-coverage" not in ran
    assert "affected-tests" in ran
    assert "SKIP cli-coverage  bazelisk run //:preflight -- --check" in captured.out
    assert "Skipping cli-coverage: no CLI test affected" in captured.err
    assert "Running cli-coverage" not in captured.err


@pytest.mark.parametrize(
    "selection",
    [
        f"//ops/tests:test_a\n{CLI_TEST}\n",
        f"@@{CLI_TEST}\n",
        "//factory/cli:lint_test\n",
        "//...\n-//ops:slow\n",
        "unreadable",
        None,
    ],
)
def test_cli_target_all_or_unreadable_selection_runs_cli_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str | None
) -> None:
    ran, _ = drive_main(tmp_path, monkeypatch, [], selection)

    assert "cli-coverage" in ran


def test_similar_label_does_not_select_cli_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran, _ = drive_main(tmp_path, monkeypatch, [], "//factory/cli-tools:test\n")

    assert "cli-coverage" not in ran


def test_explicit_check_runs_cli_coverage_without_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran, _ = drive_main(tmp_path, monkeypatch, ["--check", "cli-coverage"], None)

    assert ran == ["cli-coverage"]


def test_stage_lines_and_final_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    drive_main(tmp_path, monkeypatch, [], f"{CLI_TEST}\n")

    lines = capsys.readouterr().err.splitlines()
    assert "Running affected-tests" in lines
    assert "Finished affected-tests in 7 s (PASS)" in lines
    assert "Finished cli-coverage in 40 s (PASS)" in lines
    assert "Finished prose in 1 s (PASS)" in lines
    assert lines[-1].startswith("Preflight finished in ")
    assert lines[-1].endswith(" s: pass")


def test_failed_stage_is_reported_with_its_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = [0.0]

    async def fake_check(_name: str, _: preflight.Revisions, _scratch: Path) -> None:
        now[0] += 12.4
        raise preflight.CheckError

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.setattr(sys, "argv", ["preflight", "--check", "prose"])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(preflight, "git", AsyncMock(return_value=""))
    monkeypatch.setattr(preflight, "check", fake_check)
    monkeypatch.setattr(preflight, "clock", lambda: now[0])

    assert asyncio.run(preflight.main()) == 1

    lines = capsys.readouterr().err.splitlines()
    assert "Finished prose in 12 s (FAIL)" in lines
    assert lines[-1] == "Preflight finished in 12 s: fail"


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([("gazelle", True), ("cli-coverage", None)], "pass"),
        ([("gazelle", False), ("cli-coverage", None)], "fail gazelle"),
        ([("cli-coverage", False), ("prose", True)], "fail cli-coverage"),
    ],
)
def test_record_result_counts_a_skipped_stage_as_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    results: list[tuple[str, bool | None]],
    expected: str,
) -> None:
    head, tree, base = "a" * 40, "b" * 40, "c" * 40
    replies: dict[tuple[str, ...], str] = {
        ("rev-parse", "HEAD"): head,
        ("status", "--porcelain", "--untracked-files=no"): "",
        ("rev-parse", "--git-common-dir"): str(tmp_path),
        ("rev-parse", f"{head}^{{tree}}"): tree,
    }

    async def fake_git(*args: str) -> str:
        return replies[args]

    monkeypatch.setattr(preflight, "git", fake_git)

    asyncio.run(
        preflight.record_result(preflight.Revisions("b", base, head, "main"), results)
    )

    record = tmp_path / "tbhbdev-preflight" / head
    assert record.read_text() == f"tree {tree}\nbase {base}\nresult {expected}\n"
