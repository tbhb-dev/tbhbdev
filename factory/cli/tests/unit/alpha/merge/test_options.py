# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Pure tests for merge argument parsing and command planning."""

import pytest

from tbhbdev.alpha.merge.message import Message
from tbhbdev.alpha.merge.options import build_parser, merge_args, preview


@pytest.mark.parametrize("head", ["", "abc123", "g" * 40])
def test_merge_rejects_an_invalid_expected_head(
    capsys: pytest.CaptureFixture[str], head: str
) -> None:
    with pytest.raises(SystemExit) as info:
        build_parser().parse_args(["12", "--expect-head", head])
    assert info.value.code == 2
    error = capsys.readouterr().err
    assert "--expect-head: expected a full 40-character hexadecimal SHA" in error


def test_there_is_no_signoff_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        build_parser("merge-preview").parse_args(["12", "--dry-run", "--signoff"])
    assert info.value.code == 2
    assert capsys.readouterr().err.startswith("usage: merge-preview ")


def test_expected_head_is_lowercased() -> None:
    head = "a" * 40
    assert (
        build_parser().parse_args(["12", "--expect-head", head.upper()]).expect_head
        == head
    )


def test_preview_uses_the_explicit_message_and_verified_head() -> None:
    head = "a" * 40
    message = Message("fix: x (#12)", "Body.\n")
    argv = ["gh", *merge_args(12, head, message.subject)]
    assert preview(argv, message) == (
        "gh pr merge 12 --merge --subject 'fix: x (#12)' "
        f"--body-file - --match-head-commit {head}\n\n"
        "fix: x (#12)\n\nBody.\n"
    )
