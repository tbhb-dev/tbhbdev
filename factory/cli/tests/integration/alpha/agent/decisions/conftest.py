"""Isolate the run state and executable paths for remote script tests."""

from __future__ import annotations

lazy from pathlib import Path

import pytest
from agent_example import trusted_executables


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    trusted_executables(monkeypatch)
    state = tmp_path / "state/tbhbdev/agent-runs"
    state.mkdir(parents=True)
    (state / "sample.owner").write_text("example-owner\n")
    tools = tmp_path / "bin"
    tools.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PATH", str(tools) + ":/usr/bin:/bin")
    return tmp_path
