"""A real macOS worker is identified by its absolute executable path."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import worker_identity

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS only")
@pytest.mark.parametrize("launch", ["path", "relative"])
def test_live_a_worker_launched_by_path_or_relative_name_resolves_absolute(
    tmp_path: Path, launch: str
) -> None:
    exe = tmp_path / "review-worker"
    shutil.copy("/bin/sleep", exe)
    # A copied system binary has an invalid signature and is killed on launch.
    # cspell:ignore codesign
    subprocess.run(["/usr/bin/codesign", "--force", "-s", "-", str(exe)], check=True)
    argv0 = "review-worker" if launch == "path" else "./review-worker"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    child = subprocess.Popen([argv0, "30"], cwd=tmp_path, env=env)
    try:
        process = worker_identity.darwin_reader()(child.pid)
        assert process is not None
        assert os.path.realpath(process.exe) == os.path.realpath(exe)
    finally:
        child.kill()
        child.wait()
