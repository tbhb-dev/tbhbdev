# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Fixtures for the agent run-file protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.agent import runfiles

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def agent_suffix_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check run-file spelling on case-insensitive filesystems too."""
    original = runfiles.RunFiles.path

    def path(files: runfiles.RunFiles, suffix: str) -> Path:
        assert suffix == suffix.lower()
        return original(files, suffix)

    monkeypatch.setattr(runfiles.RunFiles, "path", path)
