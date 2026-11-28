# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise flag values and positional arguments."""

import pytest

from tbhbdev.alpha.guard import options as guard

REVIEW_VALUE_FLAGS = {"-b", "--body", "-F", "--body-file", "-R", "--repo"}


@pytest.mark.parametrize(
    ("words", "values"),
    [
        (["-X", "POST"], ["POST"]),
        (["-XPOST"], ["POST"]),
        (["--method", "POST"], ["POST"]),
        (["--method=POST"], ["POST"]),
        (["--methods=POST"], []),
        (["--method"], []),
        (["-X", "a", "x", "-X", "b"], ["a", "b"]),
    ],
)
def test_flag_values(words: list[str], values: list[str]) -> None:
    assert guard.flag_values(words, {"-X", "--method"}, {"-X", "--method"}) == values


def test_split_args_drops_flag_values() -> None:
    words = ["-b", "x", "--body=y", "7", "-a", "-R", "o/r", "-ab", "v", "-bz", "z"]
    assert guard.split_args(words, REVIEW_VALUE_FLAGS) == (
        ["-b", "--body=y", "-a", "-R", "-ab", "-bz"],
        ["7", "z"],
    )


@pytest.mark.parametrize(
    ("word", "takes"),
    [
        ("-b", True),
        ("--body", True),
        ("--body=x", False),
        ("--body-x", False),
        ("-ab", True),
        ("-ba", False),
        ("-Rx/b", False),
        ("-ca", False),
        ("-", False),
    ],
)
def test_takes_value(word: str, *, takes: bool) -> None:
    assert guard.takes_value(word, REVIEW_VALUE_FLAGS) is takes


def test_a_long_flag_never_takes_a_short_flags_value() -> None:
    assert guard.takes_value("--xb", REVIEW_VALUE_FLAGS) is False


def test_split_args_with_a_value_flag_last() -> None:
    assert guard.split_args(["7", "-b"], REVIEW_VALUE_FLAGS) == (["-b"], ["7"])


@pytest.mark.parametrize(
    ("words", "values"),
    [
        (["-X", "-f", "x"], ["-f"]),
        (["-f", "-X", "POST"], []),
        (["-H", "-Xv", "-Xw"], ["w"]),
        (["-aX", "GET"], ["GET"]),
        (["-aXv"], ["v"]),
        (["--method=", "x"], [""]),
        (["--method", "--x"], ["--x"]),
        (["--other", "-Xv"], ["v"]),
        (["x", "-", "-Xv"], ["v"]),
        (["-X"], []),
        (["aX", "v"], []),
    ],
)
def test_flag_values_skip_other_flags_values(
    words: list[str], values: list[str]
) -> None:
    flags = {"-X", "-f", "-H", "--method"}
    assert guard.flag_values(words, {"-X", "--method"}, flags) == values


@pytest.mark.parametrize(
    ("word", "found"),
    [
        ("--body", ("--body", None)),
        ("--body=", ("--body", "")),
        ("--body=x=y", ("--body", "x=y")),
        ("--body-x", None),
        ("-b", ("-b", None)),
        ("-bx", ("-b", "x")),
        ("-ab", ("-b", None)),
        ("-abR", ("-b", "R")),
        ("-a", None),
        ("-", None),
    ],
)
def test_flag_word(word: str, found: tuple[str, str | None] | None) -> None:
    assert guard.flag_word(word, REVIEW_VALUE_FLAGS) == found
