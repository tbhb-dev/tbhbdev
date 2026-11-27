# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Preview or perform a pull request merge with a clean commit message."""

import os
import sys
from typing import TYPE_CHECKING

from tbhbdev.alpha.install.git.hooks import require_git_hooks
from tbhbdev.alpha.merge.compose import build_message
from tbhbdev.alpha.merge.github import check_repository, queue_state, verify_head
from tbhbdev.alpha.merge.message import require_clean_message
from tbhbdev.alpha.merge.options import build_parser, merge_args, preview
from tbhbdev.alpha.merge.queue import queue_removal, queue_result
from tbhbdev.alpha.merge.renovate import renovate_body
from tbhbdev.alpha.merge.rules import requires_queue
from tbhbdev.alpha.pull_request import GH, find_tool, repo_visibility, run, scan_secrets

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    """Preview or merge the verified head with an explicit subject and body."""
    args = build_parser(prog).parse_args(argv)
    if not require_git_hooks(os.environ):
        return 1
    number = args.number
    request, target, rules = verify_head(number, args.expect_head)
    check_repository(target)
    queued = requires_queue(rules)
    body = renovate_body(request.body) if request.renovate else request.body
    message = build_message(
        request.title, number, body, agent=request.agent and not request.renovate
    )
    require_clean_message(message, number)
    # A MERGE queue keeps the explicit subject and body. Remember the last
    # removal so a previous event cannot be mistaken for this merge result.
    previous_removal = queue_removal(queue_state(target, number))[0] if queued else ""
    scan_secrets(
        message.text(), f"the commit message for PR #{number}", repo_visibility(target)
    )
    gh_args = merge_args(number, request.head, message.subject)
    if args.dry_run:
        print(preview([GH, *gh_args], message), end="")
        return 0
    done = run([find_tool(GH), *gh_args], stdin=message.body)
    if queued and done.returncode == 0:
        print(done.stderr, end="", file=sys.stderr)
        print(queue_result(queue_state(target, number), number, previous_removal))
        return 0
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    return done.returncode
