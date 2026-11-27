# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity commit policy."""

from __future__ import annotations

import pytest

from tbhbdev.alpha.identity.commit import policy as commit_policy


@pytest.mark.parametrize("stamp", [" 1794020000 +0000", " 1794020000 -0500"])
def test_ident_date_strips_only_the_timestamp(stamp: str) -> None:
    assert commit_policy.IDENT_DATE.sub("", f"A <a@b> 7 x{stamp}") == "A <a@b> 7 x"


@pytest.mark.parametrize(
    ("ident", "expected"),
    [
        ("A B <a@b.c> 1700000000 +0000", "a@b.c"),
        ("<x@y.z>", "x@y.z"),
        ("no address", ""),
        ("", ""),
    ],
)
def test_ident_email(ident: str, expected: str) -> None:
    assert commit_policy.ident_email(ident) == expected
