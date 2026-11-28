# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise shared shell words, options, and nested scripts."""

import pytest

from tbhbdev.alpha.guard import shell_commands as guard


def test_body_scanner_keeps_each_commands_heredoc() -> None:
    scanner = guard.BodyScanner("cat <<A\none\nA\necho\nsh <<B <<C\ntwo\nB\nthree\nC\n")
    assert scanner.run() == [["cat"], ["echo"], ["sh"]]
    assert scanner.bodies == {0: "one\nA\n", 2: "two\nB\nthree\nC\n"}
    assert scanner.pending == []


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["host", "a", "b"], "a b"),
        (["-p", "22", "host", "x"], "x"),
        (["-t", "host", "x"], "x"),
        (["host"], None),
        (["-p"], None),
        (["-o"], None),
        ([], None),
    ],
)
def test_remote_command(args: list[str], expected: str | None) -> None:
    assert guard.remote_command(args) == expected


@pytest.mark.parametrize(
    ("words", "body", "expected"),
    [
        (["bash", "-c", "x"], "y", "x"),
        (["bash"], "y", "y"),
        (["bash", "-s"], "y", "y"),
        (["bash", "f.sh"], "y", None),
        (["bash"], None, None),
        (["/bin/sh", "-c", "x"], None, "x"),
        (["eval", "a", "b"], "y", "a b"),
        (["ssh", "h", "x"], "y", "x"),
        (["ssh", "h"], "y", "y"),
        (["cat"], "y", None),
    ],
)
def test_inner_script(words: list[str], body: str | None, expected: str | None) -> None:
    assert guard.inner_script(words, body) == expected


def test_assignment_and_env_options_are_kept_as_prefix() -> None:
    found = list(guard.simple_commands("A=1 env -u NAME git commit -m x"))
    assert found == [(["A=1", "env", "-u", "NAME"], ["git", "commit", "-m", "x"])]
