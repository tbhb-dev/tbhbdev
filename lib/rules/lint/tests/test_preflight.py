# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

from lint.preflight.preflight import checks_for, summary


def test_checks_for_changed_paths() -> None:
    always = checks_for([], has_draft=False)
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
    assert checks_for(paths, has_draft=True) == (
        "affected-tests",
        "cli-coverage",
        "gitleaks-text",
        *always,
    )


def test_summary_for_mixed_results() -> None:
    assert summary([("gazelle", True), ("prose", False)], "origin/main") == (
        "PASS gazelle  bazelisk run //:preflight -- --check gazelle origin/main\n"
        "FAIL prose  bazelisk run //:preflight -- --check prose origin/main"
    )
