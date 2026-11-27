# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise account validation for shell and generated argument lists."""

import pytest

from tbhbdev.alpha.guard.op import decision as guard
from tbhbdev.alpha.guard.op.file_calls import Argv
from tbhbdev.alpha.guard.op.shell_calls import shell_calls

ACCOUNT = "example.1password.com"


@pytest.mark.parametrize(
    "flags",
    [
        "--account wrong",
        f"--account {ACCOUNT} --account wrong",
        "--account",
        "--account=",
    ],
)
def test_wrong(flags: str) -> None:
    assert any(
        not guard.allowed(words, ACCOUNT) for words in shell_calls("op read x " + flags)
    )


def test_wrong_account_with_equals() -> None:
    assert not guard.valid_account(
        ["op", "--account=wrong=example.1password.com"], ACCOUNT
    )


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["op"], False),
        (["op", "--account", ACCOUNT], True),
        (["op", f"--account={ACCOUNT}"], True),
        (["op", "--account"], False),
        (["op", "--account", "wrong"], False),
        (["op", "--account=wrong"], False),
        (["op", "--account", ACCOUNT, "--account", "wrong"], False),
        (["op", "--account", ACCOUNT, f"--account={ACCOUNT}"], True),
        (["--account=/dir/op"], False),
    ],
)
def test_account_values(words: list[str], expected: bool) -> None:
    assert guard.valid_account(words, ACCOUNT) is expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["op"], False),
        (["op", "read", "ref"], False),
        (["op", "--account", "expr"], True),
        (["op", "--account=expr"], True),
        (["op", "--accounts"], False),
        (["--account=/dir/op"], False),
    ],
)
def test_argv_flags(words: list[str], expected: bool) -> None:
    assert guard.argv_account(Argv(words)) is expected
