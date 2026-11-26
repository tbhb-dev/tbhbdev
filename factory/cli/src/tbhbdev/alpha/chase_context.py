"""Show stale pull requests on a coordination prompt."""

import json
import os
import re
import subprocess  # nosec B404
from contextlib import suppress
from pathlib import Path
from time import time
from typing import cast

from tbhbdev.alpha.agent.runfiles import atomic
from tbhbdev.alpha.repo import read_hook_stdin

WATCHDOG = (
    "WATCHDOG: these PRs have had no update for 20+ minutes. "
    "Chase each owner now, before other work:"
)
ROW = re.compile(r"^CHASE +[a-z-]+#[0-9]+ ")
NUMBER = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
AGE_FIELD = 3
MINUTES_PER_DAY = 24 * 60


def fresh_text(path: Path, minutes: int) -> str | None:
    with suppress(OSError, UnicodeError):
        # macOS find rounds whole seconds up to the next minute.
        if path.is_file() and int(time()) - int(path.stat().st_mtime) <= minutes * 60:
            return path.read_text().rstrip("\n")
    return None


def remember(path: Path, text: str) -> None:
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic(path, text)


def budget_empty(home: Path) -> bool:
    with suppress(OSError, ValueError, RecursionError):
        data: object = json.loads(
            (home / ".local/state/tbhbdev/gh-budget/core.json").read_text()
        )
        return (
            isinstance(data, dict)
            and str(cast("dict[str, object]", data).get("remaining", 1)) == "0"
        )
    return False


def recent_row(line: str) -> bool:
    if not ROW.match(line) or "renovate" in line.lower():
        return False
    fields = line.split()
    age = NUMBER.match(fields[AGE_FIELD] if len(fields) > AGE_FIELD else "")
    return age is None or float(age[0]) < MINUTES_PER_DAY


def refresh(home: Path) -> str:
    helper = os.environ.get("TBHB_STALE_PRS")
    if not helper:
        return ""
    env = os.environ | {
        "PATH": f"{home}/.local/share/tbhbdev/agent-bin:{os.environ.get('PATH', '')}"
    }
    with suppress(OSError):
        result = subprocess.run(  # nosec B603 B607
            ["timeout", "40", helper, "20"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            check=False,
        )
        rows = "\n".join(filter(recent_row, result.stdout.splitlines()))
        if rows:
            return f"{WATCHDOG}\n{rows}"
    return ""


def inject(message: str, state: Path) -> None:
    if message:
        pairs = [
            " ".join([*fields, "", ""][1:3])
            for line in message.splitlines()
            if (fields := line.split())[:1] == ["CHASE"]
        ]
        key = "".join(f"{pair};" for pair in sorted(pairs))
        previous = fresh_text(state, 20)
        if previous is None or previous.partition("\n")[0] != key:
            remember(state, f"{key}\n")
            print(message)


def main(_argv: list[str], _prog: str) -> int:
    event: dict[str, object] = {}
    with suppress(OSError, RecursionError):
        event = read_hook_stdin()
    home = Path.home()
    if event.get("cwd") != str(home / "Code/github.com/tbhb-dev"):
        return 0
    state = home / ".local/state/tbhb"
    cache = state / "chase-context.cache"
    message = fresh_text(cache, 5)
    if message is None:
        if budget_empty(home):
            return 0
        message = refresh(home)
        remember(cache, message)
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(event.get("session_id", ""))) or "none"
    directory = Path(
        os.environ.get("TBHB_CHASE_STATE") or state / "chase-context.state"
    )
    inject(message, directory / sid)
    return 0
