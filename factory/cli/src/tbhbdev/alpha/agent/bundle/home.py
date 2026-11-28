"""Paths to the active CLI bundle and its programs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

_NOT_BUILT = "not built"


def root() -> Path | None:
    raise Invalid(_NOT_BUILT)


def program() -> str:
    raise Invalid(_NOT_BUILT)


def bin_dir() -> Path | None:
    raise Invalid(_NOT_BUILT)
