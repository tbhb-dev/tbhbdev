# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Helpers for the squash merge tests."""

from __future__ import annotations

import shlex
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def write_isolated_tool(script: Path, body: str) -> None:
    """Write a standard library stand-in that skips coverage startup."""
    source = script.with_name(script.name + ".py")
    source.write_text(body)
    python = shlex.quote(sys.executable)
    path = shlex.quote(str(source))
    script.write_text(f'#!/bin/sh\nexec {python} -I -S {path} "$@"\n')
    script.chmod(0o755)
