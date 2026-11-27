# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise edit reconstruction from tool payloads and files."""

import json
from pathlib import Path

import pytest

from tbhbdev.alpha.guard.op import edits as guard


def test_patch_update_move_delete(tmp_path: Path) -> None:
    source = tmp_path / "x.sh"
    source.write_text("#!/bin/sh\necho first\necho second\n")
    patch = """*** Begin Patch
*** Update File: x.sh
@@
 #!/bin/sh
-echo first
+op read x
@@
-echo second
+echo last
*** Move to: y.sh
*** Delete File: unused.sh
*** End Patch
"""
    result = list(guard.patch_files(patch, tmp_path))
    assert result == [
        (tmp_path / "y.sh", source.read_text(), "#!/bin/sh\nop read x\necho last\n"),
        (tmp_path / "unused.sh", "", ""),
    ]
    assert source.read_text() == "#!/bin/sh\necho first\necho second\n"
    current = guard.Patch(source, "", "")
    current.line("ignored")
    assert current.after == ""


def test_replace_all() -> None:
    assert (
        guard.replacements(
            "echo x; echo y",
            [{"old_string": "echo", "new_string": "op", "replace_all": True}],
        )
        == "op x; op y"
    )


def test_patch_ordered_repeated_hunks(tmp_path: Path) -> None:
    path = tmp_path / "repeated.sh"
    before = "echo x\necho y\necho x\n"
    path.write_text(before)
    patch = """*** Begin Patch
*** Update File: repeated.sh
@@
 echo x
-echo y
+echo z
@@
-echo x
+op read x
*** End Patch
"""
    assert list(guard.patch_files(patch, tmp_path)) == [
        (path, before, "echo x\necho z\nop read x\n")
    ]


def test_patch_search_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "x: a.sh"
    before = "keep\necho x\necho x\n"
    path.write_text(before)
    patch = """*** Begin Patch
*** Update File: x: a.sh
@@
-echo x
+echo y
@@
-echo x
+op read x
*** Move to: y: a.sh
"""
    assert list(guard.patch_files(patch, tmp_path)) == [
        (tmp_path / "y: a.sh", before, "keep\necho y\nop read x\n")
    ]


@pytest.mark.parametrize("key", ["replace_all", "ReplaceAll"])
def test_multiple_replacements(key: str) -> None:
    edit: dict[str, object] = {"old_string": "x", "new_string": "y", key: True}
    assert guard.replacements("xxx", [edit]) == "yyy"
    edit[key] = False
    assert guard.replacements("xxx", [edit]) == "yxx"


def test_patch_command_field(tmp_path: Path) -> None:
    patch = "*** Begin Patch\n*** Add File: x.sh\n+op read x\n*** End Patch"
    assert list(
        guard.writes(
            {
                "cwd": str(tmp_path),
                "tool_name": "apply_patch",
                "tool_input": {"command": patch},
            }
        )
    ) == [(tmp_path / "x.sh", "", "op read x\n")]


@pytest.mark.parametrize("mode", ["replace", "insert", "delete"])
def test_notebook_cell_selection(mode: str) -> None:
    first = {"id": "first", "cell_type": "code", "source": "print(1)"}
    second = {"id": "second", "cell_type": "code", "source": "print(2)"}
    inputs: dict[str, object] = {
        "cell_id": "second",
        "edit_mode": mode,
        "new_source": "op read x",
        "cell_type": "markdown",
    }
    book_before = json.dumps({"cells": [first, second]})
    expected = [first, {**second, "source": "op read x", "cell_type": "markdown"}]
    if mode == "insert":
        expected = [first, {"cell_type": "markdown", "source": "op read x"}, second]
    elif mode == "delete":
        expected = [first]
    assert json.loads(guard.notebook_edit(book_before, inputs)) == {"cells": expected}
    assert json.loads(guard.notebook_edit(book_before, {"new_source": "updated"})) == {
        "cells": [first, second]
    }


@pytest.mark.parametrize("mode", ["replace", "insert", "delete"])
def test_unknown_notebook_cell(mode: str) -> None:
    before = json.dumps(
        {"cells": [{"id": "one", "cell_type": "code", "source": "print(1)"}]}
    )
    assert (
        guard.notebook_edit(
            before,
            {"cell_id": "unknown", "edit_mode": mode, "new_source": "!op read x"},
        )
        == before
    )


def test_patch_without_final_newline(tmp_path: Path) -> None:
    path = tmp_path / "x.sh"
    path.write_text("echo x")
    patch = (
        "*** Begin Patch\n*** Update File: x.sh\n@@\n-echo x\n+op read x\n*** End Patch"
    )
    assert list(guard.patch_files(patch, tmp_path)) == [(path, "echo x", "op read x\n")]


def test_patch_uses_context_and_separated_hunks(tmp_path: Path) -> None:
    path = tmp_path / "x.sh"
    before = "echo same\n# choose\necho same\necho keep\necho last\n"
    path.write_text(before)
    patch = """*** Begin Patch
*** Update File: x.sh
@@
 # choose
-echo same
+op read x
@@
-echo last
+op read y
*** End Patch
"""
    assert list(guard.patch_files(patch, tmp_path)) == [
        (path, before, "echo same\n# choose\nop read x\necho keep\nop read y\n")
    ]


def test_pick_optional_fields() -> None:
    assert guard.pick({}, ("content",)) is None
    assert (
        guard.pick({"content": "first", "Content": "second"}, ("content", "Content"))
        == "first"
    )


def test_empty_patch() -> None:
    assert not list(guard.patch_files("*** Begin Patch\n*** End Patch", Path.cwd()))


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_unreadable_file(tmp_path: Path, path_kind: str) -> None:
    path = tmp_path / path_kind
    if path_kind == "directory":
        path.mkdir()
    assert guard.read_file(path) == ""
