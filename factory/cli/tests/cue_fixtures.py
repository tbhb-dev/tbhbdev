# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Use the pinned CUE executable in review tests."""

import shutil
from pathlib import Path

import pytest

from tbhbdev import cue_runtime

_REVIEW_CUE = Path(shutil.which("cue") or cue_runtime.binary_path())


@pytest.fixture
def review_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bazel supplies the pinned tool on PATH in its test sandbox.
    monkeypatch.setattr(cue_runtime, "resolve", lambda: _REVIEW_CUE)
