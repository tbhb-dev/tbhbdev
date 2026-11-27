"""Install and verify a clone's managed Git hooks."""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.gh_shim import agent_operation
from tbhbdev.alpha.install.git.lock import LOCK_WAIT, hooks_lock
from tbhbdev.alpha.install.git.prek import find_prek, prek_install
from tbhbdev.alpha.install.git.shims import (
    install_identity_hook,
    install_pre_push_hook,
    require_pre_push_hook,
)
from tbhbdev.alpha.install.model import PROG, InstallError
from tbhbdev.alpha.install.scripts import IDENTITY_HOOKS, SHIM_MARK
from tbhbdev.alpha.repo import git, in_tbhb_dev

if TYPE_CHECKING:
    from collections.abc import Mapping


HOOKS_PATH = ("config", "--get", "core.hooksPath")


def install_git_hooks(cwd: Path | None, *, dry_run: bool) -> int:
    """Install the git hooks of the clone containing `cwd`."""
    code, out = git(
        "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir",
        cwd=cwd,
    )  # fmt: skip
    if code != 0:
        print(f"{PROG}: not in a git work tree; skipping the git hooks")
        return 0
    top, common = (Path(line) for line in out.split("\n"))
    if not in_tbhb_dev(top):
        print(f"{PROG}: origin is not a tbhb-dev repository; skipping the git hooks")
        return 0
    _, hooks_path = git(*HOOKS_PATH, cwd=top)
    if hooks_path:
        print(
            f"{PROG}: warning: core.hooksPath is {hooks_path};"
            " not installing git hooks",
            file=sys.stderr,
        )
        return 0
    hooks = common / "hooks"
    try:
        require_pre_push_hook(hooks)
        prek = find_prek()
        if dry_run:
            names = ", ".join(name for name, _ in IDENTITY_HOOKS)
            print(
                "would install prek's pre-commit and pre-push hooks"
                f" and the {names} identity hooks in {hooks}"
            )
            return 0
        hooks.mkdir(exist_ok=True)
        with hooks_lock(hooks, LOCK_WAIT):
            require_pre_push_hook(hooks)
            prek_install(
                prek, top, "--hook-type", "pre-commit", "--hook-type", "pre-push"
            )
            install_pre_push_hook(hooks)
            for pair in IDENTITY_HOOKS:
                install_identity_hook(prek, top, hooks, pair)
    except InstallError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 1
    print(f"{PROG}: installed git hooks in {hooks}")
    return 0


def git_hooks_problem() -> str:
    """Find a missing or ineffective hook in the directory Git actually uses."""
    code, directory = git("rev-parse", "--path-format=absolute", "--git-path", "hooks")
    if code or not directory:
        return "cannot locate Git's hooks directory"
    shims = ("pre-push", *(name for name, _ in IDENTITY_HOOKS))
    for name in (*shims, "pre-commit", "pre-push.prek"):
        hook = Path(directory) / name
        try:
            if not hook.is_file() or not os.access(hook, os.X_OK):
                return f"{hook} is missing or not executable"
            if name in shims and SHIM_MARK not in hook.read_text(errors="replace"):
                return f"{hook} is missing the tbhbdev hook"
        except OSError as exc:
            return f"cannot read {hook}: {exc.strerror}"
    return ""


def require_git_hooks(env: Mapping[str, str]) -> bool:
    """Refuse agent PR operations when the checkout's hooks cannot run."""
    if agent_operation(env) and (problem := git_hooks_problem()):
        print(
            f"{problem}. In a tbhb-dev clone run `{PROG}`. "
            "If core.hooksPath redirects Git, use the managed agent worktree "
            "or restore the clone's hooks directory.",
            file=sys.stderr,
        )
        return False
    return True
