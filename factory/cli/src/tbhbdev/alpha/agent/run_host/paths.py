"""Resolve the host paths used for a run."""

from __future__ import annotations

from pathlib import Path

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import RunPaths
lazy from tbhbdev.alpha.agent.run_host.state import Run


def planned_paths(run: Run) -> RunPaths:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    workspace = clone / ".worktrees" / f"agent-{run.files.name}"
    return RunPaths(
        str(run.home),
        str(workspace.resolve()),
        str(run.tmpdir),
        str(runfiles.state_home(run.home) / "tbhbdev" / "agent-codex" / run.files.name),
        str((run.home / run.settings.sandbox.audit_dir).resolve()),
    )
