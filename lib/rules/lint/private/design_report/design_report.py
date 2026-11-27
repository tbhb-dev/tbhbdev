# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import json
import sys
import tokenize
from pathlib import Path

from .markdown import difference
from .metrics import Report, measure


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


def snapshot(root: Path) -> Report:
    return measure(read_tree(root / "src"), read_tree(root / "tests"))


def main() -> None:
    base, head = (snapshot(Path(path)) for path in sys.argv[1:])
    for name, report in (("base", base), ("head", head)):
        data = json.dumps(report, indent=2, sort_keys=True)
        Path(f"{name}.json").write_text(data + "\n", encoding="utf-8")
    sys.stdout.write(difference(base, head))


if __name__ == "__main__":
    main()
