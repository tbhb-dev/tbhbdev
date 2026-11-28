"""Run the Git hook and harness installers."""

import os

from tbhbdev.alpha.install.arguments import build_parser
from tbhbdev.alpha.install.check_tools import install_check_tools
from tbhbdev.alpha.install.config import install_config
from tbhbdev.alpha.install.git.hooks import install_git_hooks
from tbhbdev.alpha.install.model import CLAUDE, HARNESSES
from tbhbdev.alpha.install.session import session_start
from tbhbdev.alpha.install.shims import install_gh_shim, install_ssh_shim
from tbhbdev.alpha.install.statusline.pin import install_statusline


def main(argv: list[str], prog: str) -> int:
    """Install the hooks, or preview every change for a manual dry run."""
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    env = os.environ
    if args.hook:
        return session_start(env)
    status = install_check_tools(dry_run=args.dry_run)
    status |= install_git_hooks(None, dry_run=args.dry_run)
    status |= install_gh_shim(dry_run=args.dry_run)
    status |= install_ssh_shim(dry_run=args.dry_run)
    for harness in HARNESSES:
        status |= install_config(harness, env, dry_run=args.dry_run)
        if harness == CLAUDE:
            status |= install_statusline(env, dry_run=args.dry_run)
    return status
