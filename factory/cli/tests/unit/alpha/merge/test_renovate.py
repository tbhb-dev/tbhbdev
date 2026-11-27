# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Pure tests for the body of a Renovate pull request."""

import pytest
from unit.alpha.merge.examples import RENOVATE_BODY

from tbhbdev.alpha.merge.renovate import renovate_body


def test_renovate_body_keeps_only_the_update_table() -> None:
    assert renovate_body(RENOVATE_BODY) == "- macos 15 → 26 (major)\n"


def test_renovate_body_lists_every_row_and_tolerates_missing_columns() -> None:
    body = (
        "Intro.\n\n| Package | Change |\n|---|---|\n"
        "| [a](https://x) | [`1` → `2`](https://y) |\n| b | `3` → `4` |\n"
        "\nafter\n| Package | Update | Change |\n|---|---|---|\n| c | major | 5 |\n"
    )
    assert renovate_body(body) == "- a 1 → 2\n- b 3 → 4\n"
    assert renovate_body("| Package |\n|---|\n| only |\n") == "- only\n"


def test_renovate_body_lists_a_repeated_update_once() -> None:
    body = (
        "| Package | Type | Update | Change |\n|---|---|---|---|\n"
        "| [ubuntu](https://x) |  | major | `24.04` → `26.04` |\n"
        "| [ubuntu](https://x) | github-runner | major | `24.04` → `26.04` |\n"
        "| [macos](https://x) |  | major | `15` → `26` |\n"
    )
    assert renovate_body(body) == (
        "- ubuntu 24.04 → 26.04 (major)\n- macos 15 → 26 (major)\n"
    )


@pytest.mark.parametrize(
    "body", ["", "No table here.\n", "| Name | Change |\n|---|---|\n| a | 1 |\n"]
)
def test_renovate_body_without_the_update_table_is_empty(body: str) -> None:
    assert renovate_body(body) == ""


def test_renovate_body_with_an_empty_table_is_empty() -> None:
    assert renovate_body("| Package | Change |\n|---|---|\n\ntext\n") == ""
