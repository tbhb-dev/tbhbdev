"""Refresh Git hooks and the shim path at Claude Code SessionStart."""

import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.gh_shim import bin_dir
from tbhbdev.alpha.install.git.hooks import install_git_hooks
from tbhbdev.alpha.install.model import PROG
from tbhbdev.alpha.install.scripts import path_export
from tbhbdev.alpha.repo import hook_cwd, in_tbhb_dev, read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Mapping


def export_shim_path(env: Mapping[str, str]) -> None:
    """Claude Code: put the gh shim first on PATH for the session's commands.

    Appends one export line to the SessionStart hook's ``$CLAUDE_ENV_FILE``,
    which Claude Code sources before each Bash command, unless the file
    already has it. Without the variable it does nothing. A file it can't
    read or write is reported on stderr.
    """
    target = env.get("CLAUDE_ENV_FILE")
    if not target:
        return
    path = Path(target)
    try:
        text = path.read_text() if path.exists() else ""
        if addition := path_export(bin_dir(), text):
            with path.open("a") as out:
                out.write(addition)
    except OSError as exc:
        print(f"{PROG}: cannot put the gh shim on PATH: {exc}", file=sys.stderr)


def session_start(env: Mapping[str, str]) -> int:
    """`--hook`: the shim on PATH and the payload worktree's git hooks.

    It prints on stderr only: Claude Code adds a SessionStart hook's stdout
    to the model's context. A failure is one line on stderr, and the status
    is always 0, so a broken install never blocks a session.
    """
    try:
        with redirect_stdout(sys.stderr):
            export_shim_path(env)
            cwd = hook_cwd(read_hook_stdin())
            if in_tbhb_dev(cwd):
                install_git_hooks(cwd, dry_run=False)
    except Exception as exc:  # noqa: BLE001 -- fail open on any crash
        print(f"{PROG}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0
