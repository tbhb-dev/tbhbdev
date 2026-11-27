# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Parse pull request options from argument lists."""

from pathlib import Path

from tbhbdev.alpha.pull_request.arguments import build_parser
from tbhbdev.alpha.pull_request.draft import DRAFT


def test_create_uses_the_default_draft() -> None:
    args = build_parser().parse_args(["create", "--dry-run"])
    assert (args.command, args.draft, args.dry_run) == ("create", DRAFT, True)


def test_edit_reads_a_draft_and_pull_request_number() -> None:
    args = build_parser().parse_args(["edit", "draft.md", "--pr", "7"])
    assert (args.command, args.draft, args.pr) == ("edit", Path("draft.md"), 7)
