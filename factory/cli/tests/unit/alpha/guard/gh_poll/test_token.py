# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise token names and environment option parsing."""

import pytest

from tbhbdev.alpha.guard.gh_poll import token as guard

TOKEN = "OP_SERVICE_" + "ACCOUNT_TOKEN"


@pytest.mark.parametrize(
    ("word", "args", "expected", "left"),
    [
        ("-i", [], ("i", ""), []),
        ("-", ["x"], ("i", ""), ["x"]),
        ("-iuNAME", [], ("i", ""), []),
        ("-vi", [], ("i", ""), []),
        ("-0", ["x"], ("", ""), ["x"]),
        ("-v", [], ("", ""), []),
        ("-uNAME", ["x"], ("u", "NAME"), ["x"]),
        ("-u", ["NAME", "x"], ("u", "NAME"), ["x"]),
        ("-u", [], ("u", ""), []),
        ("-vuNAME", [], ("u", "NAME"), []),
        ("-vu", ["NAME"], ("u", "NAME"), []),
        ("-uiNAME", [], ("u", "iNAME"), []),
        ("-C/work", ["x"], ("C", "/work"), ["x"]),
        ("-C", ["/work", "x"], ("C", "/work"), ["x"]),
        ("-S", ["a b"], ("S", "a b"), []),
        ("-a", ["x"], ("a", "x"), []),
        ("-P", ["x"], ("P", "x"), []),
        ("--unset=NAME", ["x"], ("u", "NAME"), ["x"]),
        ("--unset", ["NAME", "x"], ("u", "NAME"), ["x"]),
        ("--unset", [], ("u", ""), []),
        ("--uns", ["NAME"], ("u", "NAME"), []),
        ("--u=NAME", [], ("u", "NAME"), []),
        ("--chdir", ["d"], ("C", "d"), []),
        ("--chdir=d", ["x"], ("C", "d"), ["x"]),
        ("--split-string=a b", ["x"], ("S", "a b"), ["x"]),
        ("--argv0", ["a"], ("a", "a"), []),
        ("--ignore-environment", ["x"], ("i", ""), ["x"]),
        ("--ignore-e", [], ("i", ""), []),
        ("--ignore", ["x"], ("", ""), ["x"]),
        ("--i", ["x"], ("", ""), ["x"]),
        ("--debug", ["x"], ("v", ""), ["x"]),
        ("--null", ["x"], ("0", ""), ["x"]),
        ("--block-signal=INT", ["x"], ("", "INT"), ["x"]),
        ("--bogus=1", ["x"], ("", "1"), ["x"]),
        ("--", ["x"], ("", ""), ["x"]),
        ("--=x", ["y"], ("", "x"), ["y"]),
    ],
)
def test_env_option(
    word: str, args: list[str], expected: tuple[str, str], left: list[str]
) -> None:
    rest = list(args)
    assert guard.env_option(word, rest) == expected
    assert rest == left


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["-iu" + TOKEN], None),
        (["-i", "-u", "A", "x"], None),
        (["-C", "/work", "-u", TOKEN], None),
        (["--chdir=/work", "-u", TOKEN], None),
        (["-C", "/work", "-u", "A", "x"], ["x"]),
        (["-C", "-u", TOKEN], [TOKEN]),
        (["-S", f"-u {TOKEN} x"], None),
        (["-S", "-u A x", "y"], ["x", "y"]),
        (["-S", "-C", "-u", TOKEN], [TOKEN]),
        (["-S", "A=1 x", "-u", TOKEN], ["A=1", "x", "-u", TOKEN]),
        (["-S", "-i x"], None),
        (["-S", "-C /work -u A", "x"], ["x"]),
        (["-S", "-u 'a b", "x"], ["b", "x"]),
        (["-u", f"${TOKEN}"], None),
        (["-u", f"${{{TOKEN}}}"], None),
        (["-u", f"{TOKEN}x"], []),
        (["-"], None),
        ([], []),
        (["--"], []),
    ],
)
def test_env_rest_parses_options(
    options: list[str], expected: list[str] | None
) -> None:
    assert guard.env_rest(options) == expected


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        (TOKEN, True),
        (f"${TOKEN}", True),
        (f"${{{TOKEN}}}", True),
        (f"{TOKEN}=", False),
        (f"x{TOKEN}", False),
        (f"${{{TOKEN}", False),
        (f"{{{TOKEN}}}", False),
        (f"$({TOKEN})", False),
        ("", False),
    ],
)
def test_names_token(word: str, expected: bool) -> None:
    assert guard.names_token(word) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a b", ["a", "b"]),
        ("-u 'a b' c", ["-u", "a b", "c"]),
        ('a "b', ["a", '"b']),
        ("", []),
    ],
)
def test_split_string(text: str, expected: list[str]) -> None:
    assert guard.split_string(text) == expected


@pytest.mark.parametrize(
    ("program", "args", "expected"),
    [
        ("export", ["-n", TOKEN], True),
        ("export", ["-n", f"{TOKEN}=x"], True),
        ("export", ["-fn", TOKEN], True),
        ("export", ["-f", "-n", "--", TOKEN], True),
        ("export", ["-n", "A", TOKEN], True),
        ("export", ["+n", TOKEN], False),
        ("export", ["-x", TOKEN], False),
        ("export", [TOKEN, "-n"], False),
        ("export", ["-n", "A"], False),
        ("export", [f"{TOKEN}="], True),
        ("export", [f"{TOKEN}=x"], False),
        ("declare", ["+x", TOKEN], True),
        ("declare", ["+x", "A=1", TOKEN], True),
        ("declare", ["-x", TOKEN], False),
        ("declare", ["-n", TOKEN], False),
        ("typeset", ["-g", "+ix", TOKEN], True),
        ("local", ["+x", TOKEN], True),
        ("local", [f"{TOKEN}="], True),
        ("readonly", ["-n", TOKEN], False),
        ("echo", ["-n", TOKEN], False),
        ("echo", [f"{TOKEN}="], False),
        ("export", [], False),
        ("export", ["", TOKEN], False),
    ],
)
def test_clears_token(program: str, args: list[str], expected: bool) -> None:
    assert guard.clears_token(program, args) is expected


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["-u", TOKEN, "x"], None),
        (["-u", "A", "x"], ["x"]),
        (["--unset", "A", "--unset", TOKEN], None),
        ([f"--unset={TOKEN}", "x"], None),
        ([f"-u{TOKEN}"], None),
        (["-i", "--", "x"], None),
        (["--", "-i"], ["-i"]),
        (["-u"], []),
        (["x", "-u", TOKEN], ["x", "-u", TOKEN]),
    ],
)
def test_env_rest(options: list[str], expected: list[str] | None) -> None:
    assert guard.env_rest(options) == expected


def test_unwrap_of_no_words_reports_no_repeater() -> None:
    assert guard.unwrap([]) == ([], False)
