# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read preflight records for the proposed commit."""

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev.alpha import preflight_gate
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("pass", ""),
        ("absent", "no preflight result for {sha}"),
        ("other", "no preflight result for {sha}"),
        ("invalid", "no preflight result for {sha}"),
        ("base", "no preflight result for {sha}"),
        ("result", "no preflight result for {sha}"),
        ("tree", "record for {sha} names a different tree"),
        ("fail", "preflight failed at {sha}: gazelle prose"),
        ("exempt", ""),
        ("unknown", "no preflight result for {sha}"),
    ],
)
def test_problem(pr_checkout: Path, case: str, reason: str) -> None:
    _, base = git("rev-parse", "HEAD")
    source = pr_checkout / "lib/rules/lint/preflight/preflight.py"
    source.parent.mkdir(parents=True)
    source.touch()
    assert git("add", str(source))[0] == 0
    assert git("commit", "-qm", "Add preflight")[0] == 0
    _, head = git("rev-parse", "HEAD")
    _, tree = git("rev-parse", "HEAD^{tree}")
    sha = {"exempt": base, "unknown": "a" * 40}.get(case, head)
    records = pr_checkout / ".git" / "tbhbdev-preflight"
    records.mkdir()
    if case in {"pass", "other", "invalid", "tree", "fail", "base", "result"}:
        if case == "tree":
            tree = "b" * 40
        sample = f"tree {tree}\nbase {base}\nresult pass\n"
        if case == "fail":
            sample = sample.replace("result pass", "result fail gazelle prose")
        elif case == "invalid":
            sample = "result pass\n"
        elif case == "base":
            sample = sample.replace("base ", "")
        elif case == "result":
            sample = sample.replace("pass", "pending")
        (records / (base if case == "other" else sha)).write_text(
            sample, encoding="utf-8"
        )
    expected = (
        reason.format(sha=sha) + "; run: bazelisk run //:preflight" if reason else ""
    )
    assert preflight_gate.problem(sha) == expected


def test_problem_reports_git_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "a" * 40
    monkeypatch.setattr(preflight_gate, "git", Mock(return_value=(0, f".git\n{sha}")))
    probe = Mock(returncode=128, stderr=b"fatal: cannot read object\n")
    monkeypatch.setattr(preflight_gate.subprocess, "run", Mock(return_value=probe))

    assert preflight_gate.problem(sha) == (
        f"no preflight result for {sha}; run: bazelisk run //:preflight"
    )


def test_report_when_run_log_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TBHBDEV_RUN_LOG", str(tmp_path))
    preflight_gate.report("a" * 40, "")
    assert capsys.readouterr().err == f"push-gate: pass {'a' * 40}\n"
