# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Define the pull request command-line arguments."""

import argparse
from pathlib import Path

from tbhbdev.alpha.pull_request.draft import DRAFT

CLAUDE = "claude"
CODEX = "codex"
ANTIGRAVITY = "antigravity"


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog=prog, description="Check a PR draft and open the PR."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook").add_argument("harness", choices=[CLAUDE, CODEX, ANTIGRAVITY])
    sub.add_parser("ready").add_argument("pr", type=int, nargs="+")
    sub.add_parser("rebase").add_argument("pr", type=int, nargs="+")
    again = sub.add_parser("rerun")
    again.add_argument("pr", type=int)
    again.add_argument("--stuck", action="store_true")
    sub.add_parser("delete-branch").add_argument("pr", type=int)
    sub.add_parser("checks").add_argument("pr", type=int)
    # `tbhbdev alpha pr close` runs in close.py before this parser sees it, so
    # this entry only lists the verb in --help.
    sub.add_parser("close", help="close N --reason TEXT: close a pull request")
    for name in ("check", "create", "edit"):
        one = sub.add_parser(name)
        if name == "edit":
            one.add_argument("draft", type=Path)
        else:
            one.add_argument("draft", nargs="?", type=Path, default=DRAFT)
        if name == "create":
            one.add_argument("--dry-run", action="store_true")
        if name == "edit":
            one.add_argument("--pr", type=int, required=True)
    return parser
