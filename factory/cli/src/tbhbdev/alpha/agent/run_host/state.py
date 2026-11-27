"""Data shared between run-host stages."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent import credentials, runfiles
    from tbhbdev.alpha.agent.model import Host, RunPaths, RunRequest, Settings


type BazelShutdown = dict[Path, tuple[str, tuple[int, int] | None]]


@dataclass
class Run:
    files: runfiles.RunFiles
    request: RunRequest
    settings: Settings
    host: Host
    home: Path
    tmpdir: Path
    started: float
    deadline: float
    bazel_shutdown: BazelShutdown | None = None
    steer: bool = False


@dataclass(frozen=True)
class Prepared:
    paths: RunPaths
    delivery: credentials.Delivery
    auth_before: str | None = dataclasses.field(repr=False)
    bases: tuple[Path, ...] = ()
    start: str | None = None


class RunInterruptedError(Exception):
    """Escape I/O without Python retrying a built-in InterruptedError."""
