# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# ruff: noqa: INP001
"""Reject growing lint allowances between the merge base and the PR head."""

import argparse
import os
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import cast

from lint.private.check_trailing_newline import GitError, git_output

type Ceilings = dict[tuple[str, str], int]
type TomlDocument = dict[str, object]


def count_ceilings(doc: TomlDocument) -> Ceilings:
    return {
        (section, key): ceiling
        for section, entries in cast("dict[str, dict[str, int]]", doc).items()
        for key, ceiling in entries.items()
    }


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
    pylint = cast("dict[str, dict[str, int]]", tool["pylint"])
    ceilings["pylint", "max-module-lines"] = pylint["format"]["max-module-lines"]
    return dict(ceilings)


REGISTRY = ".github/lint-baselines.toml"
EXTRACTORS = {
    "counts": count_ceilings,
    "python-project": project_ceilings,
}


def growth(before: Ceilings, after: Ceilings) -> list[str]:
    return [
        f"{section} / {key} grew from {before.get((section, key), 0)} to {ceiling}"
        for (section, key), ceiling in sorted(after.items())
        if ceiling > before.get((section, key), 0)
    ]


def read_at(root: Path, revision: str, path: str) -> TomlDocument | None:
    tree = git_output(
        root, "ls-tree", "--full-tree", "--name-only", revision, "--", path
    )
    if not tree:
        return None
    return tomllib.loads(git_output(root, "show", f"{revision}:{path}").decode())


def read_ceilings(root: Path, revision: str, path: str, kind: str) -> Ceilings | None:
    doc = read_at(root, revision, path)
    return EXTRACTORS[kind](doc) if doc is not None else None


def check(root: Path, base: str, head: str) -> list[str]:
    old_registry = cast("dict[str, str]", read_at(root, base, REGISTRY) or {})
    registry = cast("dict[str, str]", read_at(root, head, REGISTRY) or {})
    removed = old_registry.items() - registry.items()
    if removed:
        return [
            f"{REGISTRY}: {path} lost its {kind} baseline"
            for path, kind in sorted(removed)
        ]

    errors = []
    for path, kind in registry.items():
        before = read_ceilings(root, base, path, kind)
        after = read_ceilings(root, head, path, kind)
        if after is None:
            errors.append(f"{path}: missing from {head}")
            continue
        if before is not None:
            errors.extend(f"{path}: {error}" for error in growth(before, after))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revisions", nargs=2, metavar=("base", "head"))
    base, head = parser.parse_args().revisions
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", "."))
    try:
        errors = check(root, base, head)
    except (GitError, KeyError, ValueError) as error:
        sys.stderr.write(f"lint_baselines: {error}\n")
        return 2
    for error in errors:
        sys.stderr.write(f"lint_baselines: {error}\n")
    return int(bool(errors))
