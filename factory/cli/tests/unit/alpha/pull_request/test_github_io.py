# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Check how the GitHub command adapter reports dry runs."""

from typing import TYPE_CHECKING

from tbhbdev.alpha.pull_request.github import invoke_gh

if TYPE_CHECKING:
    import pytest


def test_a_dry_run_reports_success_and_no_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert invoke_gh(["gh", "pr"], "b\n", dry_run=True) == (0, "")
    assert capsys.readouterr().out == "gh pr\n\nb\n"
