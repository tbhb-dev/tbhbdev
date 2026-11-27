# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity records."""

from __future__ import annotations

import hashlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from unit.alpha.identity.identity_examples import LAUNCH

from tbhbdev.alpha.identity import records
lazy from tbhbdev.alpha.identity.records import Json


@settings(database=None)
@given(st.text())
def test_sha256_64_shape(raw: str) -> None:
    hashed = records.sha256_64(raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    assert hashed == f"sha-256-64:{digest[:16]}"
    assert records.HASH_RE.fullmatch(hashed)
    assert records.hx(hashed) == digest[:16]


def test_sha256_64_hashes_the_string_form() -> None:
    assert records.sha256_64(12) == records.sha256_64("12")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("x", "x"), ("", None), (None, None), (3, None), (["x"], None)],
)
def test_as_str(value: object, expected: str | None) -> None:
    assert records.as_str(value) == expected


def test_as_dict() -> None:
    assert records.as_dict({"a": 1}) == {"a": 1}
    assert records.as_dict({}) == {}
    assert records.as_dict([1]) is None
    assert records.as_dict("x") is None


def test_first_str_takes_first_nonempty_string() -> None:
    data: Json = {"a": "", "b": 3, "c": "yes", "d": "later"}
    assert records.first_str(data, "a", "b", "c", "d") == "yes"
    assert records.first_str(data, "a", "b", "missing") is None
    assert records.first_str(data) is None


def test_hash_ok() -> None:
    assert records.hash_ok(LAUNCH) == LAUNCH
    assert records.hash_ok(LAUNCH + "0") is None
    assert records.hash_ok("x" + LAUNCH) is None
    assert records.hash_ok(None) is None


def test_parse_line_accepts_only_json_objects() -> None:
    assert records.parse_line('{"a": 1}') == {"a": 1}
    assert records.parse_line("[1]") is None
    assert records.parse_line("nope") is None
    assert records.parse_line("") is None
