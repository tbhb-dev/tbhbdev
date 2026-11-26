"""Provenance is diagnostic and remains usable without install metadata or git."""

import json
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_host_example import build_provenance, constant

from tbhbdev.alpha.agent import build_info

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("commit", ["a" * 40, "b" * 64, "", "x" * 40, "a" * 39])
def test_wheel_commit(commit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        build_info.metadata,
        "distribution",
        constant(
            SimpleNamespace(
                read_text=constant(json.dumps({"vcs_info": {"commit_id": commit}}))
            )
        ),
    )
    assert build_info.wheel_commit() == (
        commit if commit in ("a" * 40, "b" * 64) else "unknown"
    )


@pytest.mark.parametrize(
    "error", [OSError, ValueError, build_info.metadata.PackageNotFoundError]
)
def test_wheel_metadata_unavailable(
    error: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_name: str) -> None:
        raise error

    monkeypatch.setattr(build_info.metadata, "distribution", missing)
    assert build_info.wheel_commit() == "unknown"


@pytest.mark.parametrize("present", [True, False])
def test_tools_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, present: bool
) -> None:
    expected = build_provenance(tmp_path, monkeypatch, present=present)
    assert build_info.tools_commit() == expected["tools_commit"]


@pytest.mark.parametrize("error", [OSError, subprocess.SubprocessError])
def test_git_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    build_provenance(tmp_path, monkeypatch, present=True)
    missing = Mock(side_effect=error)
    monkeypatch.setattr(subprocess, "check_output", missing)
    assert build_info.tools_commit() == "unknown"
    missing.assert_called_once()
