"""Exercise Git under the run environment."""

from __future__ import annotations

import os as operating_system
import subprocess
from typing import TYPE_CHECKING

from policy_example import env_of

from tbhbdev.alpha.agent.model import (
    Harness,
    Os,
)

if TYPE_CHECKING:
    from pathlib import Path


def run_git(
    directory: Path, config: dict[str, str], *args: str, from_user: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run git in DIRECTORY with only the GIT_CONFIG_* part of a run environment."""
    env = {k: v for k, v in config.items() if k.startswith("GIT_CONFIG_")}
    env["PATH"] = operating_system.environ["PATH"]
    env["HOME"] = str(directory)
    if from_user is not None:
        env["GIT_PROTOCOL_FROM_USER"] = from_user
    return subprocess.run(  # test: fixed argv, no shell
        ["git", *args],  # test: git from PATH
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_run_may_fetch_its_own_mirror_but_not_through_git_itself(
    tmp_path: Path,
) -> None:
    config = env_of(Os.LINUX, Harness.CODEX, "agent")
    mirror = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_git(workspace, config, "init", "-q")
    run_git(workspace, config, "remote", "add", "origin", str(mirror))
    assert run_git(workspace, config, "fetch", "origin").returncode == 0
    refused = run_git(workspace, config, "fetch", "origin", from_user="0")
    assert refused.returncode != 0
    assert "transport 'file' not allowed" in refused.stderr
