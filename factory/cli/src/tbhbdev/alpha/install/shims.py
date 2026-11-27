"""Install the user-level GitHub CLI and SSH shims."""

import os
import stat
import sys

from tbhbdev.alpha.gh_shim import bin_dir, wrapper_text
from tbhbdev.alpha.install.model import PROG
from tbhbdev.alpha.install.scripts import SSH_SHIM_MARK, SSH_SHIM_TEXT

GH_SHIM_MODE = 0o755


def install_shim(
    name: str, text: str, *, dry_run: bool, mark: str | None = None
) -> int:
    """Write the agent-bin executable NAME with TEXT unless it is in place.

    With MARK, an existing file that lacks it is the user's own, so it stays.
    """
    path = bin_dir() / name
    label = f"{name}-shim"
    try:
        current = path.read_text(errors="replace") if path.is_file() else None
        if mark and current is not None and mark not in current:
            print(f"{PROG}: {label}: {path} is not ours; left alone", file=sys.stderr)
            return 1
        mode = stat.S_IMODE(path.stat().st_mode) if current is not None else None
        if current == text and mode == GH_SHIM_MODE:
            print(f"{label}: {path} is up to date")
            return 0
        if dry_run:
            print(f"{label}: would write {path}")
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
        tmp.write_text(text)
        tmp.chmod(GH_SHIM_MODE)
        tmp.replace(path)
    except OSError as exc:
        print(f"{PROG}: {label}: cannot write {path}: {exc}", file=sys.stderr)
        return 1
    print(f"{label}: wrote {path}")
    return 0


def install_gh_shim(*, dry_run: bool) -> int:
    """Write the gh shim (see gh_shim) unless it is already in place."""
    return install_shim("gh", wrapper_text(), dry_run=dry_run)


def install_ssh_shim(*, dry_run: bool) -> int:
    """Write the ssh shim: ssh with no 1Password agent, for agent sessions.

    scp and sftp call /usr/bin/ssh directly on macOS and Ubuntu, so they skip it.
    """
    return install_shim("ssh", SSH_SHIM_TEXT, dry_run=dry_run, mark=SSH_SHIM_MARK)
