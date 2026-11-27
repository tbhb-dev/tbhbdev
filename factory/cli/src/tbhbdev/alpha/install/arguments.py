"""Command-line options for installation."""

import argparse


def build_parser(prog: str) -> argparse.ArgumentParser:
    """Build the parser for manual installation and SessionStart."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Install this clone's git hooks and merge the tbhbdev alpha hooks "
            "into each harness's user-level config."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="print the changes, write nothing"
    )
    mode.add_argument(
        "--hook",
        action="store_true",
        help=(
            "SessionStart hook: put the gh shim on PATH and install the git "
            "hooks of the payload's worktree"
        ),
    )
    return parser
