"""Real copying clones and git plants exercise the harness parent's environment."""

from __future__ import annotations

import dataclasses
import fnmatch
import itertools
import subprocess
import sys
import tomllib
from pathlib import Path

import agent_git_plants
import pytest
from agent_git_plants import PLANTS
from agent_host_example import commit, git, local_mirror_fetches, make_run, repository

from tbhbdev.alpha.agent import host_tools, worktree_checkout
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Harness, Os, RunPaths
from tbhbdev.alpha.agent.run_host import command_line, environment
from tbhbdev.alpha.agent.run_host.paths import planned_paths
lazy from tbhbdev.alpha.agent.run_host.state import Run


def real_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> tuple[Run, RunPaths]:
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    run = make_run(
        tmp_path,
        harness=harness.value,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="git sandbox probe" if harness is Harness.CLAUDE else None,
    )
    run = dataclasses.replace(
        run,
        host=dataclasses.replace(
            run.host, os=Os.MACOS if sys.platform == "darwin" else Os.LINUX
        ),
    )
    parent = Path(repo.clone_path(run.home, run.request.repo)).parent
    _, clone = repository(parent)
    origin = parent / "origin"
    clone = clone.rename(parent / "example-repo")

    local_mirror_fetches(monkeypatch, origin)
    directory = tmp_path / "host-tools"
    directory.mkdir()
    prek = directory / "prek"
    prek.write_text("#!/bin/sh\nexit 0\n")
    prek.chmod(0o700)
    sandbox = dataclasses.replace(run.settings.sandbox, host_path=(str(directory),))
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, sandbox=sandbox)
    )
    with host_tools.configured(run.settings, run.host.os):
        workspace = worktree_checkout.create(clone, run.files.name, run.request)
    return run, dataclasses.replace(planned_paths(run), worktree=str(workspace))


@pytest.mark.parametrize(
    "name", [*PLANTS, "embedded-bare", "nested-config", "worktrees-entry"]
)
def test_git_parent_overrides_disable_real_plants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, paths = real_clone(tmp_path, monkeypatch, Harness.CODEX)
    marker, target = agent_git_plants.parent_plant(Path(paths.worktree), tmp_path, name)
    control = target / "nested" if name == "nested-config" else target
    options = ("-c", "safe.bareRepository=all") if name == "embedded-bare" else ()
    positive = subprocess.run(
        ["git", *options, "status", "--porcelain"],
        cwd=control,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert positive.returncode == 0, positive.stderr
    assert marker.read_text() == "invoked"
    marker.rename(tmp_path / "positive")
    env = environment.build(
        run.settings, run.host, run.request, paths, "/usr/bin:/bin"
    ).env
    for arguments in (
        ("status", "--porcelain"),
        ("diff",),
        ("diff", "--cached"),
        ("log",),
        ("show",),
    ):
        result = subprocess.run(
            ["git", *arguments],
            cwd=target,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if name == "embedded-bare":
            assert result.returncode != 0
            if arguments[0] == "status":
                assert "safe.bareRepository" in result.stderr
        else:
            assert result.returncode == 0 or (
                result.returncode == 128
                and "cannot run :" in result.stderr
                and "external diff died" in result.stderr
            ), result.stderr
        assert not marker.exists(), arguments


def test_tool_git_environment_keeps_scratch_commits_out_of_the_run_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, paths = real_clone(tmp_path, monkeypatch, Harness.CODEX)
    workspace = Path(paths.worktree)
    before = git(workspace, "rev-parse", "HEAD")
    value = environment.build(
        run.settings, run.host, run.request, paths, "/usr/bin:/bin"
    )
    command = command_line.harness(
        run.request,
        value,
        worktree=paths.worktree,
        executable="/example/codex",
        final_message="/example/output",
        stop_hooks={},
    )
    overrides = "\n".join(
        value for flag, value in itertools.pairwise(command) if flag == "-c"
    )
    shell = tomllib.loads(overrides)["shell_environment_policy"]
    inherited = value.env | {
        "GIT_DIR": str(workspace / ".git"),
        "GIT_COMMON_DIR": str(workspace / ".git"),
        "GIT_WORK_TREE": str(workspace),
        "GIT_INDEX_FILE": str(workspace / ".git/index"),
        "GIT_CONFIG_KEY_20": "core.fsmonitor",
        "GIT_CONFIG_VALUE_20": "/example/plant",
        "GIT_CONFIG_PARAMETERS": "fixture poison",
    }
    env = {
        name: value
        for name, value in inherited.items()
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in shell["exclude"])
    } | shell["set"]
    assert {name: value for name, value in env.items() if name.startswith("GIT_")} == {
        "GIT_CONFIG_COUNT": "0"
    }
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    git(scratch, "init", "--template=", ".", env=env)
    commit(scratch, "scratch", env=env)
    assert git(scratch, "log", "-1", "--format=%s") == "scratch"
    assert git(workspace, "rev-parse", "HEAD") == before
