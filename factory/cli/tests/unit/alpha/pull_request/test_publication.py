# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Interpret creation responses without contacting GitHub."""

import pytest

from tbhbdev.alpha.pull_request.publication import created_ref, creation_errors


@pytest.mark.parametrize(("status", "stream"), [(0, "stdout"), (1, "stderr")])
def test_duplicate_creation_uses_the_pull_request_number(
    status: int, stream: str
) -> None:
    output = "a pull request already exists https://github.com/example/repo/pull/17"
    stdout, stderr = (output, "") if stream == "stdout" else ("", output)
    assert creation_errors(status, stdout, stderr) == [
        output,
        (
            "gh pr create failed; run `tbhbdev alpha pr edit DRAFT --pr 17`"
            " to apply the draft"
        ),
    ]


def test_ledger_reference_comes_from_the_returned_url() -> None:
    assert created_ref("https://github.com/example/repo/pull/17\n") == "example/repo#17"
    assert created_ref("no pull request URL") is None
