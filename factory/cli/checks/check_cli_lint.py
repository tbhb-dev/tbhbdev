"""Run pylint and enforce the CLI's module and suppression ceilings."""

from __future__ import annotations

import re
import sys
import tokenize
import tomllib
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from pylint.constants import MSG_TYPES_STATUS
from pylint.lint import Run
from pylint.reporters.text import TextReporter

if TYPE_CHECKING:
    from pylint.message import Message

CLI = Path("factory/cli")
BASELINE = CLI / "lint-baseline.toml"
CONFIG = CLI / "pyproject.toml"
NOQA = re.compile(r"#\s*(?:(?:ruff|flake8):\s*)?noqa\b(?::\s*(.*))?", re.IGNORECASE)
CODES = re.compile(r"[A-Z]+\d+(?:[\s,]+[A-Z]+\d+)*", re.IGNORECASE)


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


def violations(
    files: list[Path], ceilings: dict[str, int], allowed: dict[str, int], limit: int
) -> list[str]:
    errors = []
    counts: Counter[str] = Counter()
    for path in files:
        if not path.is_relative_to(CLI):
            continue
        relative = path.relative_to(CLI)
        if relative.parts[0] not in {"src", "tests"}:
            continue
        name = relative.as_posix()
        content = path.read_bytes()
        lines = len(content.splitlines())
        ceiling = ceilings.get(name, limit)
        if lines > ceiling:
            errors.append(f"{path}: {lines} lines (ceiling {ceiling})")
        counts.update(suppressions(content))
    for rule, count in sorted(counts.items()):
        ceiling = allowed.get(rule, 0)
        if count > ceiling:
            errors.append(
                f"{CLI}: {rule} has {count} noqa suppressions (ceiling {ceiling})"
            )
    return errors


def pylint(files: list[str], ceilings: dict[str, int]) -> int:
    class BaselineReporter(TextReporter):
        reported = False

        def handle_message(self, msg: Message) -> None:
            if msg.symbol == "too-many-lines":
                name = Path(msg.abspath).relative_to(CLI.resolve(), walk_up=True)
                if name.as_posix() in ceilings:
                    return
            self.reported = True
            super().handle_message(msg)

    reporter = BaselineReporter()
    run = Run(
        [f"--rcfile={CONFIG}", "--score=n", *files], reporter=reporter, exit=False
    )
    # Pylint still sets the convention bit for messages the reporter skips.
    status = run.linter.msg_status
    return 0 if status == MSG_TYPES_STATUS["C"] and not reporter.reported else status


def main() -> int:
    files = sys.argv[1:]
    baseline = tomllib.loads(BASELINE.read_text(encoding="utf-8"))
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    ceilings = baseline["module_lines"]
    limit = config["tool"]["pylint"]["format"]["max-module-lines"]
    errors = violations(
        [Path(name) for name in files], ceilings, baseline["noqa"], limit
    )
    for error in errors:
        sys.stderr.write(f"{error}\n")
    return pylint(files, ceilings) or int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
