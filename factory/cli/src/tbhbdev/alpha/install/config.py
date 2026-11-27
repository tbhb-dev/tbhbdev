"""Install user-level JSON hook configurations."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.install.codex import trust_codex_hooks
from tbhbdev.alpha.install.files import read_config, write_config
from tbhbdev.alpha.install.model import CODEX, PROG, InstallError
from tbhbdev.alpha.install.settings import (
    config_diff,
    config_path,
    format_config,
    merged_config,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def install_config(harness: str, env: Mapping[str, str], *, dry_run: bool) -> int:
    """Merge one harness's hooks into its user-level config.

    Codex's hooks are then trusted in its `config.toml`, whether or not
    `hooks.json` changed.
    """
    path = config_path(harness, env, Path.home())
    try:
        old, config = read_config(path)
        merged = merged_config(harness, config)
    except InstallError as exc:
        print(f"{PROG}: {harness}: cannot merge into {path}: {exc}", file=sys.stderr)
        return 1
    if old is not None and merged == config:
        print(f"{harness}: {path} is up to date")
        status = 0
    else:
        new = format_config(merged)
        status = apply_config(harness, path, old, new, dry_run=dry_run)
    if harness == CODEX and status == 0:
        status = trust_codex_hooks(path, merged, env, dry_run=dry_run)
    return status


def apply_config(
    harness: str, path: Path, old: str | None, new: str, *, dry_run: bool
) -> int:
    """Print the change to a config on a dry run; otherwise write it."""
    if dry_run:
        print(config_diff(path, old, new), end="")
        return 0
    try:
        backup = write_config(path, old, new)
    except InstallError as exc:
        print(f"{PROG}: {harness}: cannot write {path}: {exc}", file=sys.stderr)
        return 1
    saved = f"; the old one is {backup}" if backup else ""
    print(f"{harness}: wrote {path}{saved}")
    return 0
