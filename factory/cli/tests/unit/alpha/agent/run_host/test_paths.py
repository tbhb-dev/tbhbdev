"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_host_example import make_run
from run_host_fixtures import HOST_FIXTURES

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import (
    RunPaths,
)
from tbhbdev.alpha.agent.run_host import dispatcher as host_dispatcher
from tbhbdev.alpha.agent.run_host import paths as host_paths

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


@pytest.mark.parametrize("common", [False, True])
def test_planned_paths_ignore_account_git_directory_data(
    tmp_path: Path, common: bool
) -> None:
    run = make_run(tmp_path)
    clone = Path(repo.clone_path(run.home, run.request.repo))
    clone.mkdir(parents=True)
    gitdir = run.home / "gitdir"
    gitdir.mkdir()
    (clone / ".git").write_text(f"gitdir: {gitdir}\n")
    if common:
        (gitdir / "commondir").write_text("../shared\n")
    paths = host_paths.planned_paths(run)
    assert paths == RunPaths(
        home=str(run.home),
        worktree=str(clone / ".worktrees" / "agent-example"),
        tmpdir=str(run.tmpdir),
        codex_home=str(tmp_path / "state" / "tbhbdev" / "agent-codex" / run.files.name),
        audit_dir=str(run.home / run.settings.sandbox.audit_dir),
    )


def test_empty_state_home_uses_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert host_dispatcher.main(["example"], "example run-host") == 1
    assert (
        Path.home() / ".local/state/tbhbdev/agent-runs/example.rc"
    ).read_text() == "1\n"


@pytest.mark.parametrize("state", [None, ""])
def test_codex_home_defaults_to_account_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    run = make_run(tmp_path)
    if state is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", state)
    assert host_paths.planned_paths(run).codex_home == str(
        run.home / ".local/state/tbhbdev/agent-codex/example"
    )
