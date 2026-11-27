# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# ruff: noqa: INP001
"""Read Git output as bytes."""

import subprocess  # nosec B404
lazy from pathlib import Path

GitError = subprocess.CalledProcessError


def git_output(root: Path, *args: str) -> bytes:
    return subprocess.check_output(  # noqa: S603  # nosec B603, B607
        ["git", *args],  # noqa: S607
        cwd=root,
    )
