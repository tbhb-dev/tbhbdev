# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Fixtures for the merge command's executable stand-ins."""

import os
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from fake_tool import (
    PRIVATE_CONFIG,
    RULES_BEYOND_ONE,
    link_git,
    write_private_config,
    write_test_hooks,
)
from unit.merge.fake_github import COG, GH, GITLEAKS, Tools, amend, queue_data

from tbhbdev.alpha import tool_pins

if TYPE_CHECKING:
    from pathlib import Path


def write_isolated_tool(script: Path, body: str) -> None:
    """Write a standard library stand-in that skips coverage startup."""
    source = script.with_name(script.name + ".py")
    source.write_text(body)
    python = shlex.quote(sys.executable)
    path = shlex.quote(str(source))
    script.write_text(f'#!/bin/sh\nexec {python} -I -S {path} "$@"\n')
    script.chmod(0o755)


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tools:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_dir = tmp_path / "git"
    link_git(git_dir)
    version = tool_pins.pinned_version("gitleaks")
    for name, body in (("gh", GH), ("cog", COG), ("gitleaks", GITLEAKS)):
        write_isolated_tool(bin_dir / name, body.format(version=version))
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    prefixes = ("CLAUDE", "CODEX", "ANTIGRAVITY", "GIT_")
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(prefixes)]))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{git_dir}")
    # The scan runs from the repository root and needs both configs.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitleaks.toml").write_text("")
    monkeypatch.chdir(repo)
    subprocess.run(["git", "init", "-q", "--template="], check=True)
    write_test_hooks(repo / ".git/hooks")
    write_private_config(tmp_path, PRIVATE_CONFIG + RULES_BEYOND_ONE)
    made = Tools(bin_dir)
    made.view()
    return made


@pytest.fixture
def queued_tools(tools: Tools) -> Tools:
    tools.view(title="fix: tidy", body="A plain body.\n", baseRefName="release/One")
    amend(
        tools,
        rules_out='[[{"type":"pull_request"}],'
        '[{"type":"merge_queue","parameters":{"merge_method":"MERGE"}}]]',
        queue_before_out=queue_data(
            removal={"id": "old", "reason": "An earlier removal"},
        ),
        queue_out=queue_data(state="OPEN", isInMergeQueue=True),
    )
    return tools
