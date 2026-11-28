"""Paths to the active CLI bundle and its programs."""

from __future__ import annotations

from pathlib import Path

import tbhbdev

_BUNDLE_SUFFIX = ".cli"
_RUNS_DIR = "agent-runs"
_LIB_DIR = "lib"
_BIN_DIR = "bin"
_PROGRAM = "tbhbdev"


def module_file() -> str | None:
    return tbhbdev.__file__


def root() -> Path | None:
    location = module_file()
    if location is None:
        return None
    path = Path(location)
    if not path.is_absolute():
        return None
    for folder in path.parents:
        bundle = folder.parent
        if (
            folder.name == _LIB_DIR
            and bundle.suffix == _BUNDLE_SUFFIX
            and bundle.parent.name == _RUNS_DIR
        ):
            return bundle
    return None


def program() -> str:
    directory = bin_dir()
    return _PROGRAM if directory is None else str(directory / _PROGRAM)


def bin_dir() -> Path | None:
    bundle = root()
    return None if bundle is None else bundle / _BIN_DIR
