# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Isolated repositories and hook runners for identity tests."""

import json
import os
import shlex
import subprocess
import sys
import time
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from fake_tool import pythonpath
from integration.alpha.identity.identity_test_support import (
    AGENT_VARS,
    COG_LIKE,
    EARLY,
    HUMAN_COMMIT_ENV,
    LAUNCH,
    NOW,
    PREK_CONFIG,
    SRC,
    git_commit,
    hook_env,
    init_repo,
    returning,
    run_git,
    stage,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(scope="session")
def fake_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A `tbhbdev` on PATH that runs this source tree, as the hooks expect."""
    path = tmp_path_factory.mktemp("bin")
    tool = path / "tbhbdev"
    tool.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(pythonpath(SRC))} "
        f'exec {shlex.quote(sys.executable)} -m tbhbdev "$@"\n',
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_bin: Path) -> None:
    """No injected GIT_* or agent variables, no user git config, a temp HOME."""
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(AGENT_VARS)]))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # A hook with no cwd reads and writes the cwd's repository: keep it out of ours.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    # The agent identity, which commit-msg requires of a commit in a harness.
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "tbhb.dev Agent")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "agent@tonyburns.net")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = init_repo(tmp_path / "repo")
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory outside any repository, as the working directory."""
    path = tmp_path / "plain"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tbhb-dev repository with `tbhbdev alpha install`'s git hooks."""
    path = init_repo(tmp_path / "wired")
    (path / ".pre-commit-config.yaml").write_text(
        yaml.safe_dump(PREK_CONFIG), encoding="utf-8"
    )
    stage(path, "base.txt")
    run_git(path, "add", ".pre-commit-config.yaml")
    run_git(path, "commit", "-q", "-m", "init")
    monkeypatch.chdir(path)
    done = subprocess.run(
        ["tbhbdev", "alpha", "install"],
        capture_output=True,
        text=True,
        check=False,
        env=hook_env(),
    )
    assert done.returncode == 0, done.stderr
    return path


@pytest.fixture
def with_msg_linter(wired: Path) -> Path:
    """``wired`` plus a prek commit-msg hook (a cog-like linter), reinstalled."""
    (wired / "cog.sh").write_text(COG_LIKE, encoding="utf-8")
    config = wired / ".pre-commit-config.yaml"
    loaded_config = cast("dict[str, list[object]]", yaml.safe_load(config.read_text()))
    loaded_config["repos"].append(
        {
            "repo": "local",
            "hooks": [
                {
                    "id": "cog",
                    "name": "cog verify",
                    "language": "system",
                    "entry": "sh cog.sh",
                    "stages": ["commit-msg"],
                }
            ],
        }
    )
    config.write_text(yaml.safe_dump(loaded_config), encoding="utf-8")
    subprocess.run(
        ["tbhbdev", "alpha", "install"],
        check=True,
        capture_output=True,
        env=hook_env(),
    )
    run_git(wired, "add", "cog.sh", ".pre-commit-config.yaml")
    done = git_commit(wired, "chore: linter", HUMAN_COMMIT_ENV)
    assert done.returncode == 0, done.stderr
    (wired / "cog.log").unlink(missing_ok=True)
    return wired


@pytest.fixture
def launch_file(repo: Path) -> Path:
    """The launch file a launcher writes before its harness starts."""
    path = repo / ".git" / "tbhb-alpha" / "launch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"launch": LAUNCH, "started_at": EARLY}))
    return path


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", returning(NOW))
