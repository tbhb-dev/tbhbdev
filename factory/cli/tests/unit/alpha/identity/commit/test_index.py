# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for parsing staged Git index entries."""

from tbhbdev.alpha.identity.commit.index import index_blobs


def test_index_uses_stage_zero_and_preserves_paths() -> None:
    index = "100644 abc 1\tconflict\x00100644 def 0\ta\tfile\0malformed\0"
    assert index_blobs(index) == {"a\tfile": "def"}
