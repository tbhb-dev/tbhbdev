"""Isolate host Git and its mirror when exercising worktree preparation."""

lazy from pathlib import Path

import pytest
from agent_host_example import isolate_host_git, local_mirror_fetches

from tbhbdev.alpha.agent import host_tools


@pytest.fixture(autouse=True)
def scratch_cwd(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.path.stem not in {
        "test_worktree_checkout",
        "test_worktree_clone",
        "test_worktree_git",
        "test_worktree_hooks",
        "test_worktree_mirror",
    }:
        return
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name in ("CACHE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    isolate_host_git(tmp_path, monkeypatch)

    local_mirror_fetches(monkeypatch, tmp_path / "origin")
    resolve = host_tools.resolve

    def executable(name: str, path: str | None = None) -> str:
        return "/example/prek" if name == "prek" else resolve(name, path)

    monkeypatch.setattr(host_tools, "resolve", executable)
