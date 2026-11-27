# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import argparse
import json
import sys
import tokenize
from pathlib import Path

from markdown import difference
from metrics import measure


def read_tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise NotADirectoryError(root)
    package = [root.name] if (root / "__init__.py").is_file() else []
    modules = {}
    for path in sorted(root.rglob("*.py")):
        name = ".".join([*package, *path.relative_to(root).with_suffix("").parts])
        with tokenize.open(path) as file:
            modules[name] = file.read()
    return modules


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure Python design metrics and report changes."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("measure", help="Write a JSON snapshot to stdout.")
    snapshot.add_argument("source", type=Path)
    snapshot.add_argument("tests", type=Path)
    comparison = commands.add_parser("diff", help="Write a Markdown comparison to stdout.")
    comparison.add_argument("base", type=Path)
    comparison.add_argument("head", type=Path)
    args = parser.parse_args()
    if args.command == "measure":
        result = measure(read_tree(args.source), read_tree(args.tests))
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        base = json.loads(args.base.read_text(encoding="utf-8"))
        head = json.loads(args.head.read_text(encoding="utf-8"))
        sys.stdout.write(difference(base, head))


if __name__ == "__main__":
    main()
