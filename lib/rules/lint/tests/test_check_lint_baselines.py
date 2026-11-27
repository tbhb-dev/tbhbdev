# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

lazy from pathlib import Path

import pytest

from lint.check_lint_baselines import check, go_source_metrics, source_metrics
from lint.private.git_read import git_output

CLI = "factory/cli"
CONFIG = f"{CLI}/pyproject.toml"
GO_PROJECT = "factory/example"
GO_CONFIG = f"{GO_PROJECT}/go-lint-baselines.toml"
GO_LIMITS = "max-module-lines = 400\nmax-test-lines = 800\n"


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


def configure(
    repo: Path, edge: str, module_limit: int = 400, test_limit: int = 800
) -> None:
    write(
        repo,
        CONFIG,
        f"""
[[tool.importlinter.contracts]]
id = "example"
ignore_imports = ["{edge}"]

[tool.lint-baselines]
max-module-lines = {module_limit}
max-test-lines = {test_limit}
""",
    )


def test_lint_baseline_growth(repo: Path) -> None:
    configure(repo, "a -> b")
    base = commit(repo)
    configure(repo, "a -> c", 401, 801)
    head = commit(repo)
    assert check(repo, base, head) == [
        f"{CONFIG}: example / a -> c grew from 0 to 1",
        f"{CONFIG}: lint-baselines / max-module-lines grew from 400 to 401",
        f"{CONFIG}: lint-baselines / max-test-lines grew from 800 to 801",
    ]


def test_lint_baselines_with_legacy_config(repo: Path) -> None:
    write(
        repo,
        CONFIG,
        """
[[tool.importlinter.contracts]]
id = "example"
ignore_imports = ["a -> b"]

[tool.pylint.format]
max-module-lines = 400
""",
    )
    base = commit(repo)
    configure(repo, "a -> b")
    assert check(repo, base, commit(repo)) == []


@pytest.mark.parametrize(("directory", "limit"), [("src", 400), ("tests", 800)])
def test_code_line_limits(repo: Path, directory: str, limit: int) -> None:
    configure(repo, "a -> b")
    base = commit(repo)
    path = f"{CLI}/{directory}/new.py"
    content = (
        '''# Module comment
"""A module docstring.

Its second paragraph.
"""

class Example:
    """A class docstring."""

    async def method(self):
        # Method comment
        """A method docstring."""
        pass  # Inline comment
'''
        + "pass\n" * (limit - 3)
    )
    write(repo, path, content)
    assert check(repo, base, commit(repo)) == []

    write(repo, path, content + "pass\n")
    assert check(repo, base, commit(repo)) == [
        f"{path}: {limit + 1} code lines (ceiling {limit})",
    ]


@pytest.mark.parametrize(("directory", "limit"), [("src", 400), ("tests", 800)])
def test_oversized_file_cannot_grow(repo: Path, directory: str, limit: int) -> None:
    configure(repo, "a -> b")
    path = f"{CLI}/{directory}/existing.py"
    oversized = "pass\n" * (limit + 1)
    write(repo, path, oversized)
    base = commit(repo)

    write(repo, path, '"""Added documentation."""\n# Added comment\n\n' + oversized)
    assert check(repo, base, commit(repo)) == []

    write(repo, path, oversized + "pass\n")
    assert check(repo, base, commit(repo)) == [
        f"{path}: {limit + 2} code lines (ceiling {limit + 1})",
    ]

    write(repo, path, "pass\n" * limit)
    assert check(repo, base, commit(repo)) == []


def test_count_strings_with_code() -> None:
    content = '''# coding: latin-1
"""Module docstring
"""; pass
def été(): ("First" " sentence."); pass
def other():
    ("docstring"
     " continuation")
    text = """A multiline value.

# This is part of the value.
"""
    "A string after the first statement."
if True:
    "A string in an if block."
def formatted():
    f"A string with {1}."
'''
    assert source_metrics(content.encode("latin-1")) == (11, {})


def test_count_with_bom_and_crlf() -> None:
    content = '"""Docstring.\r\n""";  # noqa: S105\r\n\r\npass\r\n'
    assert source_metrics(content.encode("utf-8-sig")) == (1, {"S105": 1})


def test_suppressions_follow_git_base(repo: Path) -> None:
    existing = f"{CLI}/src/existing.py"
    added = f"{CLI}/tests/new.py"
    write(repo, existing, "pass  # noqa: S105\n")
    old_base = commit(repo)

    write(repo, existing, "pass  # noqa: S105, S106\n")
    base = commit(repo)

    configure(repo, "a -> b")
    write(repo, added, 'text = "# noqa: S106"\n')
    head = commit(repo)
    assert check(repo, base, head) == []
    assert check(repo, old_base, head) == [
        f"{CLI}: S106 has 1 noqa suppressions (ceiling 0)",
    ]

    write(repo, existing, "pass  # noqa: S105\n")
    write(repo, added, "pass  # noqa: S105\n# ruff: noqa\n")
    head = commit(repo)
    assert check(repo, base, head) == [
        f"{CLI}: S105 has 2 noqa suppressions (ceiling 1)",
        f"{CLI}: all has 1 noqa suppressions (ceiling 0)",
    ]

    write(repo, existing, "pass\n")
    (repo / added).unlink()
    assert check(repo, base, commit(repo)) == []


def test_go_count_comments_and_literals() -> None:
    content = r"""//go:build linux

/* Header
//nolint:ignored
*/
package example /* trailing comment */
var text = "before // after \"//nolint:ignored"
var slash = '/' //nolint:errcheck, gosec // explanation
var raw = `literal
//nolint:ignored
// Code generated example. DO NOT EDIT.
`
/* comment */ var value = 1 + /* starts
comment */ 2 // trailing comment
//nolint // explanation
//nolint:errcheck,errcheck
//nolint:all
//nolinting is ordinary prose
"""
    assert go_source_metrics(content.encode("utf-8-sig").replace(b"\n", b"\r\n")) == (
        9,
        {"errcheck": 2, "gosec": 1, "(bare)": 1, "all": 1},
    )


def test_go_generated_header() -> None:
    assert (
        go_source_metrics(
            b"//go:build linux\n\n// Code generated by a tool. DO NOT EDIT.\n"
            b"package example //nolint\n"
        )
        is None
    )
    assert go_source_metrics(
        b"/*\n// Code generated by a tool. DO NOT EDIT.\n*/\n"
        b"package example\n// Code generated by a tool. DO NOT EDIT.\n"
    ) == (1, {})


@pytest.mark.parametrize(
    ("filename", "limit"), [("file_linux.go", 400), ("file_test.go", 800)]
)
def test_go_code_line_limits(repo: Path, filename: str, limit: int) -> None:
    path = f"{GO_PROJECT}/internal/{filename}"
    header = "//go:build linux\n\npackage example\n"
    regular = header + "var _ = 0\n" * (limit - 1)
    write(repo, path, regular + "var _ = 1\n")
    base = commit(repo)

    write(repo, GO_CONFIG, GO_LIMITS)
    write(repo, path, regular + "var _ = 1 // comment\n")
    write(
        repo,
        f"{GO_PROJECT}/generated.go",
        "// Code generated by a tool. DO NOT EDIT.\n" + regular * 3 + "//nolint\n",
    )
    assert check(repo, base, commit(repo)) == []

    write(repo, path, regular + "var _ = 1\nvar _ = 2\n")
    new_path = f"{GO_PROJECT}/new_{filename}"
    write(repo, new_path, regular + "var _ = 1\n")
    assert check(repo, base, commit(repo)) == [
        f"{path}: {limit + 2} code lines (ceiling {limit + 1})",
        f"{new_path}: {limit + 1} code lines (ceiling {limit})",
    ]


def test_go_suppressions_are_per_linter_and_project(repo: Path) -> None:
    path = f"{GO_PROJECT}/file.go"
    other = "factory/other"
    write(repo, f"{other}/go-lint-baselines.toml", GO_LIMITS)
    write(repo, path, "package example //nolint:errcheck\n")
    write(repo, f"{other}/file.go", "package other //nolint:gosec\n")
    base = commit(repo)

    write(repo, GO_CONFIG, GO_LIMITS)
    write(repo, path, "package example //nolint:errcheck,gosec\n//nolint\n")
    write(repo, f"{other}/file.go", "package other\n")
    assert check(repo, base, commit(repo)) == [
        f"{GO_PROJECT}: (bare) has 1 nolint suppressions (ceiling 0)",
        f"{GO_PROJECT}: gosec has 1 nolint suppressions (ceiling 0)",
    ]


def test_go_limits_cannot_increase_or_disappear(repo: Path) -> None:
    write(repo, GO_CONFIG, GO_LIMITS)
    base = commit(repo)
    write(repo, GO_CONFIG, GO_LIMITS.replace("400", "401"))
    assert check(repo, base, commit(repo)) == [
        f"{GO_CONFIG}: lint-baselines / max-module-lines grew from 400 to 401",
    ]

    (repo / GO_CONFIG).unlink()
    head = commit(repo)
    assert check(repo, base, head) == [f"{GO_CONFIG}: missing from {head}"]


def test_go_suppressions_use_merge_base(repo: Path) -> None:
    path = f"{GO_PROJECT}/file.go"
    write(repo, GO_CONFIG, GO_LIMITS)
    write(repo, path, "package example\n")
    fork = commit(repo)
    write(repo, path, "package example //nolint:gosec\n")
    base = commit(repo)
    git_output(repo, "checkout", "--detach", fork)
    write(repo, path, "package example\n//nolint:gosec\n")
    assert check(repo, base, commit(repo)) == [
        f"{GO_PROJECT}: gosec has 1 nolint suppressions (ceiling 0)",
    ]
