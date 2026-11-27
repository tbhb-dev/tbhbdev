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
type TomlDocument = dict[str, object]


CLI = "factory/cli"
CLI_CONFIG = f"{CLI}/pyproject.toml"
LIMIT_KEYS = {"src": "max-module-lines", "tests": "max-test-lines"}
NOQA = re.compile(r"#\s*(?:(?:ruff|flake8):\s*)?noqa\b(?::\s*(.*))?", re.IGNORECASE)
CODES = re.compile(r"[A-Z]+\d+(?:[\s,]+[A-Z]+\d+)*", re.IGNORECASE)


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
        key = LIMIT_KEYS["src"]
        ceilings["lint-baselines", key] = pylint["format"][key]
    else:
        for key in LIMIT_KEYS.values():
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


def project_metrics(root: Path, revision: str) -> tuple[dict[str, int], Counter[str]]:
    entries = git_output(
        root,
        "ls-tree",
        "-rz",
        "--full-tree",
        revision,
        "--",
        f"{CLI}/src",
        f"{CLI}/tests",
    )
    lengths: dict[str, int] = {}
    counts: Counter[str] = Counter()
    for entry in entries.split(b"\0")[:-1]:
        metadata, path = entry.split(b"\t", 1)
        if metadata.startswith(b"100") and path.endswith(b".py"):
            content = git_output(
                root, "cat-file", "blob", metadata.split()[-1].decode()
            )
            lines, file_counts = source_metrics(content)
            lengths[os.fsdecode(path)] = lines
            counts.update(file_counts)
    return lengths, counts


def file_growth(root: Path, base: str, head: str, limits: dict[str, int]) -> list[str]:
    old_lengths, old_counts = project_metrics(root, base)
    lengths, counts = project_metrics(root, head)
    errors = []
    for path, lines in lengths.items():
        directory = path.removeprefix(f"{CLI}/").split("/", 1)[0]
        ceiling = max(limits[directory], old_lengths.get(path, 0))
        if lines > ceiling:
            errors.append(f"{path}: {lines} code lines (ceiling {ceiling})")
    for rule, count in sorted(counts.items()):
        ceiling = old_counts[rule]
        if count > ceiling:
            errors.append(
                f"{CLI}: {rule} has {count} noqa suppressions (ceiling {ceiling})"
            )
    return errors


def growth(before: Ceilings, after: Ceilings) -> list[str]:
    return [
        f"{section} / {key} grew from {before.get((section, key), 0)} to {ceiling}"
        for (section, key), ceiling in sorted(after.items())
        if ceiling > before.get((section, key), 0)
    ]


def read_config(root: Path, revision: str) -> TomlDocument | None:
    tree = git_output(
        root, "ls-tree", "--full-tree", "--name-only", revision, "--", CLI_CONFIG
    )
    if not tree:
        return None
    return tomllib.loads(git_output(root, "show", f"{revision}:{CLI_CONFIG}").decode())


def check(root: Path, base: str, head: str) -> list[str]:
    old_doc = read_config(root, base)
    doc = read_config(root, head)
    after = project_ceilings(doc) if doc is not None else None
    if doc is None or after is None:
        return [f"{CLI_CONFIG}: missing from {head}"]

    before = project_ceilings(old_doc) if old_doc is not None else None
    limits = {
        directory: after["lint-baselines", key] for directory, key in LIMIT_KEYS.items()
    }
    errors = []
    if before is not None:
        before.setdefault(("lint-baselines", "max-test-lines"), limits["tests"])
        errors.extend(f"{CLI_CONFIG}: {error}" for error in growth(before, after))
    errors.extend(file_growth(root, base, head, limits))
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
