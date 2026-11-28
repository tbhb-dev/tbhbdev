# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import asyncio
import sys
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from lint.preflight import preflight

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
