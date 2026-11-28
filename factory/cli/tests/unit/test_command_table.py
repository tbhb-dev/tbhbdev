# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Unit tests for the lazy command table."""

from __future__ import annotations

import pytest

from tbhbdev.command_table import CommandTable


def sample_command(_argv: list[str], _prog: str) -> int:
    return 0


def replacement_command(_argv: list[str], _prog: str) -> int:
    return 1


def test_membership_iteration_and_length_do_not_load() -> None:
    loaded: list[str] = []
    table = CommandTable({"a": lambda: (loaded.append("a"), sample_command)[1]})
    assert "a" in table
    assert "missing" not in table
    assert list(table) == ["a"]
    assert len(table) == 1
    assert table.get("missing") is None
    assert loaded == []
    assert table["a"] is sample_command
    assert loaded == ["a"]


def test_overrides_and_deletion_work_without_loading_other_entries() -> None:
    loaded: list[str] = []
    table = CommandTable(
        {
            "a": lambda: (loaded.append("a"), sample_command)[1],
            "b": lambda: (loaded.append("b"), sample_command)[1],
        }
    )
    table["extra"] = replacement_command
    assert "extra" in table
    assert len(table) == 3
    assert set(table) == {"a", "b", "extra"}
    assert table["extra"] is replacement_command
    assert loaded == []

    table["a"] = replacement_command
    assert len(table) == 3
    assert table["a"] is replacement_command
    assert loaded == []

    del table["extra"]
    assert "extra" not in table
    assert len(table) == 2

    del table["a"]
    assert table["a"] is sample_command
    assert loaded == ["a"]

    with pytest.raises(KeyError):
        _ = table["nope"]
