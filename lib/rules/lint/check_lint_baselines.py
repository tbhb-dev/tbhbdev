# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# ruff: noqa: INP001
"""Reject growing lint allowances between the merge base and the PR head."""

import argparse
import os
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

type Ceilings = dict[tuple[str, str], int]


def count_ceilings(doc: dict[str, Any]) -> Ceilings:
    return {
        (section, key): ceiling
        for section, entries in doc.items()
        for key, ceiling in entries.items()
    }


def project_ceilings(doc: dict[str, Any]) -> Ceilings | None:
    tool = doc.get("tool", {})
    if "importlinter" not in tool:
        return None
    ceilings = Counter(
        (contract["id"], edge)
        for contract in tool["importlinter"]["contracts"]
        for edge in contract.get("ignore_imports", [])
    )
    ceilings["pylint", "max-module-lines"] = tool["pylint"]["format"][
        "max-module-lines"
    ]
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


def read_at(root: Path, revision: str, path: str) -> dict[str, Any] | None:
    def git(*args: str) -> bytes:
        return subprocess.check_output(  # noqa: S603  # nosec B603, B607
            ["git", *args],  # noqa: S607
            cwd=root,
        )

    tree = git("ls-tree", "--full-tree", "--name-only", revision, "--", path)
    return tomllib.loads(git("show", f"{revision}:{path}").decode()) if tree else None


def check(root: Path, base: str, head: str) -> list[str]:
    old_registry = read_at(root, base, REGISTRY) or {}
    registry = read_at(root, head, REGISTRY) or {}
    removed = old_registry.items() - registry.items()
    if removed:
        return [
            f"{REGISTRY}: {path} lost its {kind} baseline"
            for path, kind in sorted(removed)
        ]

    errors = []
    for path, kind in registry.items():
        extract = EXTRACTORS[kind]
        old_doc = read_at(root, base, path)
        new_doc = read_at(root, head, path)
        before = extract(old_doc) if old_doc is not None else None
        after = extract(new_doc) if new_doc is not None else None
        if after is None:
            errors.append(f"{path}: missing from {head}")
            continue
        if before is not None:
            errors.extend(f"{path}: {error}" for error in growth(before, after))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("head")
    args = parser.parse_args()
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", "."))
    try:
        errors = check(root, args.base, args.head)
    except (subprocess.CalledProcessError, KeyError, ValueError) as error:
        sys.stderr.write(f"lint_baselines: {error}\n")
        return 2
    for error in errors:
        sys.stderr.write(f"lint_baselines: {error}\n")
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
