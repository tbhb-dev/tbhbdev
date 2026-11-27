# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request branch rules."""

import pytest
from unit.alpha.pull_request.pr_draft_samples import (
    REBASE_BODY,
    TICKED_REBASE_BODY,
    TICKED_REBASE_LINE,
)

from tbhbdev.alpha.pull_request import branch_rules as pr_branch_rules


def test_refusal_reason_decodes_invalid_bytes_and_joins_streams() -> None:
    assert pr_branch_rules.refusal_reason(b"bad \xff byte\n", b"and\r\nmore") == (
        "bad \ufffd byte and more"
    )


REBASE_LOGIN = "tbhb-dev-renovate[bot]"


@pytest.mark.parametrize(
    "rewritten",
    [
        REBASE_BODY,
        TICKED_REBASE_BODY + "\n",
        TICKED_REBASE_BODY.replace("by Renovate", "by upstream"),
        TICKED_REBASE_BODY.replace(
            f"{TICKED_REBASE_LINE}\n\n", f"{TICKED_REBASE_LINE}\n\r"
        ),
    ],
)
def test_rebase_verifies_the_whole_body_before_writing(rewritten: str) -> None:
    with pytest.raises(
        SystemExit, match=r"^PR 8: cannot verify a checkbox-only change$"
    ):
        pr_branch_rules.rebase_body(
            8, REBASE_BODY, replace_checkbox=lambda _body, _line: rewritten
        )
