# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# ruff: noqa: INP001
"""Reject growing lint allowances between the merge base and the PR head."""

import argparse
import os
import re
import sys
import tokenize
import tomllib
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import cast

from lint.private.git_read import GitError, git_output

type Ceilings = dict[tuple[str, str], int]
type TomlDocument = dict[str, object]


CLI = "factory/cli"
CLI_CONFIG = f"{CLI}/pyproject.toml"
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
    ceilings["pylint", "max-module-lines"] = module_limit(doc)
    return dict(ceilings)


def module_limit(doc: TomlDocument) -> int:
    tool = cast("dict[str, dict[str, dict[str, int]]]", doc["tool"])
    return tool["pylint"]["format"]["max-module-lines"]


def suppressions(content: bytes) -> Counter[str]:
    counts: Counter[str] = Counter()
    for token in tokenize.tokenize(BytesIO(content).readline):
        if token.type != tokenize.COMMENT or not (
            directive := NOQA.search(token.string)
        ):
            continue
        codes = CODES.match(directive[1] or "")
        counts.update(
            set(re.findall(r"[A-Z]+\d+", codes[0].upper())) if codes else {"all"}
        )
    return counts


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
            lengths[os.fsdecode(path)] = len(content.splitlines())
            counts.update(suppressions(content))
    return lengths, counts


def file_growth(root: Path, base: str, head: str, limit: int) -> list[str]:
    old_lengths, old_counts = project_metrics(root, base)
    lengths, counts = project_metrics(root, head)
    errors = []
    for path, lines in lengths.items():
        ceiling = max(limit, old_lengths.get(path, 0))
        if lines > ceiling:
            errors.append(f"{path}: {lines} lines (ceiling {ceiling})")
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
    errors = []
    if before is not None:
        errors.extend(f"{CLI_CONFIG}: {error}" for error in growth(before, after))
    errors.extend(file_growth(root, base, head, module_limit(doc)))
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
