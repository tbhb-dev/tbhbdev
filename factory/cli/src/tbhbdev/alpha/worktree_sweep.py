"""Run the daily worktree classifier and apply its plan."""

import os
import subprocess  # nosec B404
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def run(log: Path, env: Mapping[str, str], *argv: Path) -> None:
    with log.open("wb") as stream:
        try:
            subprocess.run(  # nosec B603
                argv, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False
            )
        except OSError as exc:
            stream.write(f"{argv[0]}: {exc.strerror}\n".encode())


def main(_argv: list[str], _prog: str) -> int:
    home = Path.home()
    env = os.environ | {
        "PATH": f"/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:/usr/bin:/bin"
    }
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state")
    stamp = datetime.fromtimestamp(time(), UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = state / "tbhb/2026-11-13-tools-worktree-sweep" / stamp
    directory.mkdir(parents=True, exist_ok=True)
    libexec = home / ".local/libexec/tbhb"
    run(
        directory / "classify.log",
        env,
        libexec / "2026-11-13-tools-worktree-classify-all.sh",
        directory,
    )
    apply_output = directory / "apply.out"
    run(
        apply_output,
        env,
        libexec / "2026-11-13-tools-worktree-apply.sh",
        directory / "plan.tsv",
        directory / "apply.log",
    )
    sys.stdout.buffer.write(apply_output.read_bytes())
    return 0
