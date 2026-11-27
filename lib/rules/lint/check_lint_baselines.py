# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# ruff: noqa: INP001
"""Reject growing lint allowances between the merge base and the PR head."""

import argparse
import ast
import os
import re
import sys
import tokenize
import tomllib
from codecs import BOM_UTF8
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import cast

from lint.private.git_read import GitError, git_output

type Ceilings = dict[tuple[str, str], int]
type Metrics = tuple[dict[str, int], Counter[str]]
type TomlDocument = dict[str, object]


MODULE_LIMIT = "max-module-lines"
TEST_LIMIT = "max-test-lines"
CONFIG_PATH = re.compile(r"factory/[^/]+/(pyproject\.toml|go-lint-baselines\.toml)")
NOQA = re.compile(r"#\s*(?:(?:ruff|flake8):\s*)?noqa\b(?::\s*(.*))?", re.IGNORECASE)
CODES = re.compile(r"[A-Z]+\d+(?:[\s,]+[A-Z]+\d+)*", re.IGNORECASE)
NOLINT = re.compile(r"//[ \t]*nolint(?=$|[ \t:]|//)(?::([^/]*))?")
GO_GENERATED = re.compile(r"// Code generated .* DO NOT EDIT\.")
GO_TOKEN = re.compile(
    r"""//[^\n]*|/\*[\s\S]*?\*/|`[^`]*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'"""
)


def project_ceilings(doc: TomlDocument) -> Ceilings | None:
    tool = cast("TomlDocument", doc.get("tool", {}))
    imports = tool.get("importlinter")
    if imports is None:
        return None
    contracts = cast("dict[str, list[TomlDocument]]", imports)["contracts"]
    ceilings = Counter(
        (cast("str", contract["id"]), edge)
        for contract in contracts
        for edge in cast("list[str]", contract.get("ignore_imports", []))
    )
    limits = cast("dict[str, int] | None", tool.get("lint-baselines"))
    if limits is None:
        # Older revisions kept the module limit in Pylint's format table.
        pylint = cast("dict[str, dict[str, int]]", tool["pylint"])
        ceilings["lint-baselines", MODULE_LIMIT] = pylint["format"][MODULE_LIMIT]
    else:
        for key in (MODULE_LIMIT, TEST_LIMIT):
            ceilings["lint-baselines", key] = limits[key]
    return dict(ceilings)


def remove_docstrings(tree: ast.Module, lines: list[str]) -> None:
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        statement = node.body[0]
        first = statement.lineno - 1
        last = cast("int", statement.end_lineno) - 1
        # AST columns are UTF-8 byte offsets, even for other source encodings.
        prefix = lines[first].encode()[: statement.col_offset].decode()
        suffix = lines[last].encode()[statement.end_col_offset :].decode()
        lines[first] = prefix
        lines[first + 1 : last + 1] = [""] * (last - first)
        lines[last] += suffix.lstrip().removeprefix(";")


def source_metrics(content: bytes) -> tuple[int, Counter[str]]:
    content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    tokens = tokenize.tokenize(BytesIO(content).readline)
    lines = content.removeprefix(BOM_UTF8).decode(next(tokens).string).split("\n")
    counts: Counter[str] = Counter()
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        row, column = token.start
        lines[row - 1] = lines[row - 1][:column]
        if not (directive := NOQA.search(token.string)):
            continue
        codes = CODES.match(directive[1] or "")
        counts.update(
            set(re.findall(r"[A-Z]+\d+", codes[0].upper())) if codes else {"all"}
        )
    remove_docstrings(ast.parse(content), lines)
    return sum(1 for line in lines if line.strip()), counts


def go_source_metrics(content: bytes) -> tuple[int, Counter[str]] | None:
    # Match literals with comments so comment markers inside literals stay code.
    text = content.decode("utf-8-sig").replace("\r\n", "\n")
    code: list[str] = []
    counts: Counter[str] = Counter()
    end = 0
    for token in GO_TOKEN.finditer(text):
        start = token.start()
        code.append(text[end:start])
        end = token.end()
        value = token[0]
        if value.startswith("//"):
            if (
                GO_GENERATED.fullmatch(value)
                and (start == 0 or text[start - 1] == "\n")
                and not any(part.strip() for part in code)
            ):
                return None
            if directive := NOLINT.match(value):
                names = {
                    name.strip().lower() for name in (directive[1] or "").split(",")
                }
                counts.update(names - {""} or {"(bare)"})
        elif value.startswith("/*"):
            code.append("\n" * value.count("\n"))
        else:
            code.append(value)
    code.append(text[end:])
    return sum(1 for line in "".join(code).split("\n") if line.strip()), counts


def project_metrics(
    root: Path, revision: str, project: str, *, is_python: bool
) -> Metrics:
    paths = [f"{project}/src", f"{project}/tests"] if is_python else [project]
    suffix = b".py" if is_python else b".go"
    measure = source_metrics if is_python else go_source_metrics
    entries = git_output(root, "ls-tree", "-rz", "--full-tree", revision, "--", *paths)
    lengths: dict[str, int] = {}
    counts: Counter[str] = Counter()
    for entry in entries.split(b"\0")[:-1]:
        metadata, path = entry.split(b"\t", 1)
        if metadata.startswith(b"100") and path.endswith(suffix):
            content = git_output(
                root, "cat-file", "blob", metadata.split()[-1].decode()
            )
            if (metrics := measure(content)) is not None:
                lines, file_counts = metrics
                lengths[os.fsdecode(path)] = lines
                counts.update(file_counts)
    return lengths, counts


def file_growth(
    project: str, limits: Ceilings, before: Metrics, after: Metrics, *, is_python: bool
) -> list[str]:
    old_lengths, old_counts = before
    lengths, counts = after
    errors = []
    for path, lines in lengths.items():
        is_test = (
            path.startswith(f"{project}/tests/")
            if is_python
            else path.endswith("_test.go")
        )
        key = TEST_LIMIT if is_test else MODULE_LIMIT
        ceiling = max(limits["lint-baselines", key], old_lengths.get(path, 0))
        if lines > ceiling:
            errors.append(f"{path}: {lines} code lines (ceiling {ceiling})")
    suppression = "noqa" if is_python else "nolint"
    for rule, count in sorted(counts.items()):
        ceiling = old_counts[rule]
        if count > ceiling:
            errors.append(
                f"{project}: {rule} has {count} {suppression} suppressions"
                f" (ceiling {ceiling})"
            )
    return errors


def growth(before: Ceilings, after: Ceilings) -> list[str]:
    return [
        f"{section} / {key} grew from {before.get((section, key), 0)} to {ceiling}"
        for (section, key), ceiling in sorted(after.items())
        if ceiling > before.get((section, key), 0)
    ]


def read_configs(root: Path, revision: str) -> dict[str, Ceilings]:
    tree = git_output(
        root, "ls-tree", "-rz", "--full-tree", "--name-only", revision, "--", "factory"
    )
    configs = {}
    for path in map(os.fsdecode, tree.split(b"\0")[:-1]):
        if not CONFIG_PATH.fullmatch(path):
            continue
        doc = tomllib.loads(git_output(root, "show", f"{revision}:{path}").decode())
        if path.endswith("/pyproject.toml"):
            if (ceilings := project_ceilings(doc)) is not None:
                configs[path] = ceilings
        else:
            limits = {}
            for key in (MODULE_LIMIT, TEST_LIMIT):
                limit = doc.get(key)
                if type(limit) is not int or limit <= 0:
                    msg = f"{path}: {key} must be a positive integer"
                    raise ValueError(msg)
                limits["lint-baselines", key] = limit
            configs[path] = limits
    return configs


def check(root: Path, base: str, head: str) -> list[str]:
    base = git_output(root, "merge-base", "--", base, head).decode().strip()
    old_configs = read_configs(root, base)
    configs = read_configs(root, head)
    errors = []
    for config in sorted(old_configs.keys() | configs.keys()):
        if config not in configs:
            errors.append(f"{config}: missing from {head}")
            continue
        project, filename = config.rsplit("/", 1)
        is_python = filename == "pyproject.toml"
        after = configs[config]
        if config in old_configs:
            before = old_configs[config]
            before.setdefault(
                ("lint-baselines", TEST_LIMIT), after["lint-baselines", TEST_LIMIT]
            )
            errors.extend(f"{config}: {error}" for error in growth(before, after))
        errors.extend(
            file_growth(
                project,
                after,
                project_metrics(root, base, project, is_python=is_python),
                project_metrics(root, head, project, is_python=is_python),
                is_python=is_python,
            )
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revisions", nargs=2, metavar=("base", "head"))
    base, head = parser.parse_args().revisions
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", "."))
    try:
        errors = check(root, base, head)
    except (GitError, KeyError, SyntaxError, tokenize.TokenError, ValueError) as error:
        sys.stderr.write(f"lint_baselines: {error}\n")
        return 2
    for error in errors:
        sys.stderr.write(f"lint_baselines: {error}\n")
    return int(bool(errors))
