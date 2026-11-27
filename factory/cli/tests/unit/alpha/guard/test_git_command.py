# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise Git global option parsing."""

from tbhbdev.alpha.guard.git_command import subcommand


def test_global_flags_before_the_verb() -> None:
    assert subcommand(["git", "-C", "work", "--no-pager", "commit"]) == (4, "commit")


def test_no_verb_after_a_value_option() -> None:
    assert subcommand(["git", "-c", "key=value"]) == (3, "")
