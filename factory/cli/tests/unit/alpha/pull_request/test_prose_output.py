# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request prose output."""

import pytest

from tbhbdev.alpha.pull_request import prose_output as pr_prose_output


def test_cspell_and_rumdl_lines_ignore_other_output() -> None:
    parse = pr_prose_output.parse_lines(pr_prose_output.CSPELL_LINE, "cspell")
    assert parse("noise\nCSpell: Files checked: 1\n") == []
    assert parse("a.md:3:4 - Unknown word (x)") == [(3, "cspell: Unknown word (x)")]


@pytest.mark.parametrize(
    "output", ["{", "[]", '{"draft.md": {}}', '{"draft.md": [{}]}']
)
def test_vale_parse_raises_on_bad_json(output: str) -> None:
    with pytest.raises(ValueError, match=r"Expecting|expected Vale alerts"):
        pr_prose_output.parse_vale(output)


def test_vale_parse_accepts_an_empty_result() -> None:
    assert pr_prose_output.parse_vale("{}") == []
