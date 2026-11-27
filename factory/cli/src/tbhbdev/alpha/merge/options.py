# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse merge options and build the head-pinned gh invocation."""

import argparse
import shlex
from typing import TYPE_CHECKING

from tbhbdev.alpha.merge.review_gate import HEAD_SHA

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tbhbdev.alpha.merge.message import Message


def parse_head(value: str) -> str:
    """Read a full commit SHA in GitHub's lowercase form.

    Raises:
        argparse.ArgumentTypeError: The value is not a full hexadecimal SHA.
    """
    head = value.lower()
    if not HEAD_SHA.fullmatch(head):
        message = "expected a full 40-character hexadecimal SHA"
        raise argparse.ArgumentTypeError(message)
    return head


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Merge a PR with a clean merge commit message.",
    )
    parser.add_argument("number", type=int, metavar="PR")
    parser.add_argument(
        "--expect-head",
        type=parse_head,
        metavar="SHA",
        help="also require this approved 40-character SHA",
    )
    parser.add_argument("--dry-run", action="store_true", help="show it, merge nothing")
    return parser


def merge_args(number: int, head: str, subject: str) -> list[str]:
    """Pass the verified head and explicit commit message to gh."""
    return [
        "pr", "merge", str(number), "--merge", "--subject", subject,
        "--body-file", "-", "--match-head-commit", head,
    ]  # fmt: skip


def preview(argv: Sequence[str], message: Message) -> str:
    """Show the command and commit message used for this merge."""
    return f"{shlex.join(argv)}\n\n{message.text()}"
