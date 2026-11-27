"""Write Codex hook trust and shell environment in one edit."""

import sys
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.dispatcher import codex_home
from tbhbdev.alpha.install.environment import with_environment
from tbhbdev.alpha.install.files import read_text, write_config
from tbhbdev.alpha.install.model import PROG, InstallError
from tbhbdev.alpha.install.settings import config_diff
from tbhbdev.alpha.install.trust import our_hashes, trusted_text

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from tbhbdev.alpha.install.model import Json


def trust_codex_hooks(
    hooks_path: Path, merged: Json, env: Mapping[str, str], *, dry_run: bool
) -> int:
    """Trust each Codex hook of ours in `config.toml`, and only those.

    Codex runs a hook only when `config.toml` holds its hash. A new file is
    private and an existing one keeps its mode.

    The same write sets the shell environment when its table can be merged.
    """
    path = codex_home(env) / "config.toml"
    hooks = hooks_path.absolute()
    wanted = our_hashes(hooks, merged)
    status = 0
    try:
        old = read_text(path)
        new = trusted_text(old, hooks, wanted)
        try:
            new = with_environment(new)
        except InstallError as exc:
            print(f"{PROG}: codex: cannot merge into {path}: {exc}", file=sys.stderr)
            status = 1
        if old == new:
            print(f"codex: {len(wanted)} hooks already trusted in {path}")
            return status
        if dry_run:
            print(config_diff(path, old, new), end="")
            return status
        backup = write_config(path, old, new, new_file_mode=0o600)
    except (InstallError, ValueError) as exc:
        print(f"{PROG}: codex: cannot trust hooks in {path}: {exc}", file=sys.stderr)
        return 1
    saved = f"; the old one is {backup}" if backup else ""
    print(f"codex: trusted {len(wanted)} hooks in {path}{saved}")
    return status
