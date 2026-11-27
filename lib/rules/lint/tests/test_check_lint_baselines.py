# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

lazy from pathlib import Path

import pytest

from lint.check_lint_baselines import check
from lint.private.git_read import git_output

CLI = "factory/cli"
CONFIG = f"{CLI}/pyproject.toml"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git_output(tmp_path, "init", "-q")
    git_output(tmp_path, "config", "user.name", "Test User")
    git_output(tmp_path, "config", "user.email", "test@example.com")
    git_output(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def write(repo: Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def commit(repo: Path) -> str:
    git_output(repo, "add", ".")
    git_output(repo, "commit", "-qm", "lint fixture")
    return git_output(repo, "rev-parse", "HEAD").decode().strip()


def configure(repo: Path, edge: str, limit: int = 300) -> None:
    write(
        repo,
        CONFIG,
        f"""
[[tool.importlinter.contracts]]
id = "example"
ignore_imports = ["{edge}"]

[tool.pylint.format]
max-module-lines = {limit}
""",
    )


def test_lint_baseline_growth(repo: Path) -> None:
    configure(repo, "a -> b")
    base = commit(repo)
    configure(repo, "a -> c", 301)
    head = commit(repo)
    assert check(repo, base, head) == [
        f"{CONFIG}: example / a -> c grew from 0 to 1",
        f"{CONFIG}: pylint / max-module-lines grew from 300 to 301",
    ]


def test_file_allowances_follow_git_base(repo: Path) -> None:
    existing = f"{CLI}/src/existing.py"
    added = f"{CLI}/tests/new.py"
    write(repo, existing, "pass  # noqa: S105\n" + "\n" * 300)
    old_base = commit(repo)

    write(repo, existing, "pass  # noqa: S105, S106\n" + "\n" * 301)
    base = commit(repo)

    configure(repo, "a -> b")
    write(repo, added, 'text = "# noqa: S106"\n' + "\n" * 299)
    head = commit(repo)
    assert check(repo, base, head) == []
    assert check(repo, old_base, head) == [
        f"{existing}: 302 lines (ceiling 301)",
        f"{CLI}: S106 has 1 noqa suppressions (ceiling 0)",
    ]

    write(repo, existing, "pass  # noqa: S105\n" + "\n" * 302)
    write(repo, added, "pass  # noqa: S105\n# ruff: noqa\n" + "\n" * 299)
    head = commit(repo)
    assert check(repo, base, head) == [
        f"{existing}: 303 lines (ceiling 302)",
        f"{added}: 301 lines (ceiling 300)",
        f"{CLI}: S105 has 2 noqa suppressions (ceiling 1)",
        f"{CLI}: all has 1 noqa suppressions (ceiling 0)",
    ]

    write(repo, existing, "pass\n" + "\n" * 300)
    (repo / added).unlink()
    assert check(repo, base, commit(repo)) == []
