"""Exercise Git credential resolution in a build clone."""

import os
import subprocess
lazy from pathlib import Path

from agent_host_example import git, isolate_host_git
lazy import pytest

from tbhbdev.alpha.agent import worktree
from tbhbdev.alpha.agent.model import Profile


def test_build_git_uses_the_run_gh_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_host_git(tmp_path, monkeypatch)
    git(tmp_path, "init", "--bare", "mirror")
    clone = tmp_path / "clone"
    worktree.clone_run(
        tmp_path / "mirror", clone, "https://example.test/repo.git", Profile.BUILD
    )
    helper = tmp_path / "gh"
    helper.write_text('#!/bin/sh\ncat "$GH_CONFIG_DIR/credential"\n')
    helper.chmod(0o700)
    value = "username=example\npassword=example-run-credential\n"
    (tmp_path / "credential").write_text(value)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    config = tmp_path / "global"
    config.write_text('[credential]\nhelper = "!echo username=x; echo password=x"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    output = subprocess.check_output(
        ["git", "-C", str(clone), "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        timeout=10,
    )
    assert output.endswith(value)
