"""Tests for shell literal quoting in Connect token scripts."""

from tbhbops_cli.token_shell import quote


def test_quoting_keeps_a_single_quote_inside_a_quoted_name() -> None:
    assert quote("a'b") == "'a'\\''b'"
    assert quote("ab") == "'ab'"
