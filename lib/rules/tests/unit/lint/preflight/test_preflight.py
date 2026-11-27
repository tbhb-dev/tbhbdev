# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import asyncio
import sys
import tempfile
from typing import TYPE_CHECKING

import pytest

from lint.preflight import preflight

if TYPE_CHECKING:
    from pathlib import Path


def test_checks_for_changed_paths() -> None:
    always = preflight.checks_for([], has_draft=False)
    assert set(always) == {
        "actionlint",
        "agent-capacity-images",
        "agent-capacity-modules",
        "agent-capacity-vulnerabilities",
        "design-report",
        "gazelle",
        "github-metrics-vulnerabilities",
        "gitleaks-history",
        "lint-baselines",
        "module-tidy",
        "pr-size",
        "prose",
    }
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


@pytest.mark.parametrize(
    ("check_name", "report_name"),
    [
        ("design-report", "design-report.md"),
        ("agent-capacity-modules", "gomodscan.sarif"),
    ],
)
def test_main_keeps_report(
    check_name: str,
    report_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_git(*args: str) -> str:
        return "revision" if args[0] in {"merge-base", "rev-parse"} else ""

    async def fake_check(name: str, _: preflight.Revisions, scratch: Path) -> None:
        assert name == check_name
        (scratch / report_name).write_text("report\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.setattr(sys, "argv", ["preflight", "--check", check_name])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(preflight, "git", fake_git)
    monkeypatch.setattr(preflight, "check", fake_check)

    assert asyncio.run(preflight.main()) == 0

    (report,) = tmp_path.glob(f"preflight-*/{report_name}")
    assert report.read_text(encoding="utf-8") == "report\n"
    assert str(report.parent) in capsys.readouterr().out
