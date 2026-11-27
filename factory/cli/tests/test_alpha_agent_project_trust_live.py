"""Opt-in check of the Codex project configuration trust override."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from agent_host_example import make_run
from agent_live_evidence import codex_auth

from tbhbdev.alpha.agent import credentials
from tbhbdev.alpha.agent.run_host.harness import harness_command
from tbhbdev.alpha.agent.run_host.paths import planned_paths
from tbhbdev.alpha.agent.run_host.preflight import resolve_harness
from tbhbdev.alpha.agent.run_host.state import Prepared

live_only = pytest.mark.skipif(
    os.environ.get("TBHBDEV_CODEX_TRUST_LIVE") != "1",
    reason="set TBHBDEV_CODEX_TRUST_LIVE=1 to run the Codex project trust probe",
)


@live_only
def test_codex_exec_refuses_project_config(tmp_path: Path) -> None:
    installed = shutil.which("codex")
    if installed is None:
        pytest.fail("codex: harness binary not installed")
    run = make_run(tmp_path)
    paths = planned_paths(run)
    workspace = Path(paths.worktree)
    workspace.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--template=", str(workspace)], check=True, capture_output=True
    )
    project = workspace / ".codex"
    project.mkdir()
    (project / "config.toml").write_text("invalid = [")
    run.files.write("prompt", "probe")
    value, executable = resolve_harness(run, paths)
    prepared = Prepared(paths, credentials.Delivery({}, None, None), None)
    command = harness_command(run, prepared, value, executable)
    command = (
        *command[:-1],
        "-c",
        'model_provider="example-nonexistent-provider"',
        command[-1],
    )
    scratch_home = tmp_path / "scratch-home"
    scratch_home.mkdir()
    environment = value.env | {
        "HOME": str(scratch_home),
        "CODEX_HOME": paths.codex_home,
    }
    with codex_auth(run, paths, run.home, tmp_path):
        untrusted = subprocess.run(
            command,
            input="probe",
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert "example-nonexistent-provider" in untrusted.stderr
        assert "model provider" in untrusted.stderr.lower()
        assert "not found" in untrusted.stderr.lower()
        assert "toml parse error" not in untrusted.stderr.lower()
        trusted_command = tuple(
            arg.replace('trust_level="untrusted"', 'trust_level="trusted"')
            for arg in command
        )
        trusted = subprocess.run(
            trusted_command,
            input="probe",
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert trusted.returncode != 0
        assert "toml parse error" in trusted.stderr.lower()
        assert str(project / "config.toml") in trusted.stderr
        assert "example-nonexistent-provider" not in trusted.stderr
