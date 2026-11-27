# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise GitHub global flag parsing."""

from tbhbdev.alpha.guard.gh_options import after_global_options


def test_global_options_consume_separate_values() -> None:
    args = ["-q", "-R", "example/repo", "--hostname", "example", "pr", "view"]
    assert after_global_options(args) == ["pr", "view"]


def test_global_options_alone_have_no_subcommand() -> None:
    assert after_global_options(["--repo"]) == []
