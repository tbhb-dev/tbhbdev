# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity claude payload."""

from __future__ import annotations

from tbhbdev.alpha.identity.claude import payload as claude_payload


def test_diff_and_walk_paths() -> None:
    text = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n+++ /dev/null\n"
    assert claude_payload.diff_paths(text) == ["x.txt", "/dev/null", "x.txt"]
    assert claude_payload.diff_paths("+++ plain.txt\n") == ["plain.txt"]
    assert claude_payload.diff_paths("++++ nope\n+++ \n") == []
    assert claude_payload.diff_paths("") == []
    assert claude_payload.walk_paths(text) == claude_payload.diff_paths(text)
    assert claude_payload.walk_paths(3) == []
    assert claude_payload.walk_paths(None) == []
    nested = [
        {"filePath": "/r/a.py", "patch": [{"path": "p"}, {"file_path": "q"}]},
        "+++ b/z",
        {"other": 5, "filePath": ""},
    ]
    assert claude_payload.walk_paths(nested) == ["/r/a.py", "p", "q", "z"]
    assert claude_payload.walk_paths({"path": "a", "filePath": "b"}) == ["b", "a"]


def test_edit_paths_from_bash() -> None:
    assert claude_payload.edit_paths_from_bash({}) == []
    assert claude_payload.edit_paths_from_bash(None) == []
    assert claude_payload.edit_paths_from_bash("text") == []
    assert claude_payload.edit_paths_from_bash({"bashEditDiff": None}) == []
    diff = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n"
    assert claude_payload.edit_paths_from_bash({"bashEditDiff": diff}) == ["x.txt"]
    assert claude_payload.edit_paths_from_bash(
        {"bashEditDiff": [{"filePath": "/r/a.py"}]}
    ) == ["/r/a.py"]
    assert claude_payload.edit_paths_from_bash(
        {"bashEditDiff": "+++ /dev/null\n+++ a\n+++ a\n"}
    ) == ["a"]
    assert claude_payload.edit_paths_from_bash({"bashEditDiff": ""}) == []
