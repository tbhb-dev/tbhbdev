"""Scratch run files and shell execution for remote script tests."""

from __future__ import annotations

import os
import shlex
import subprocess
lazy from pathlib import Path

from agent_driver_example import write_standalone_fake
from agent_example import settings

from tbhbdev.alpha.agent.decisions import remote_cleanup


def with_test_paths(script: str) -> str:
    return (
        script.replace(
            "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "PATH=" + os.environ["PATH"]
        )
        .replace('PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"', 'PATH="$PATH"')
        .replace(settings().sockets_dir, os.environ["HOME"] + "/sockets")
    )


def execute(script: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["/bin/sh", "-c", with_test_paths(script)],
        input=b"example-owner\n",
        capture_output=True,
        check=False,
    )


async def execute_ssh(args: tuple[str, ...], _data: bytes) -> tuple[int, bytes]:
    command = shlex.split(shlex.split(args[-1])[2])
    result = execute(command[2])
    return result.returncode, result.stdout


def no_session(host: Path) -> None:
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )


def clone_in(host: Path) -> Path:
    clone = host / "Code/github.com/example-org/example-repo"
    tree = clone / ".worktrees/agent-sample"
    (tree / ".git").mkdir(parents=True)
    (tree / "agent-file").write_text("preserve")
    return clone


def cleanup(*, force: bool = False) -> str:
    return remote_cleanup.clean(
        "example-org/example-repo",
        "sample",
        "stamp",
        settings().sockets_dir,
        force=force,
    )
