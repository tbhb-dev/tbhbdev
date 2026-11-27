# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read original file contents and simulate proposed edits."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.dispatcher import agy_edit_path, agy_tool
from tbhbdev.alpha.repo import hook_cwd

if TYPE_CHECKING:
    from collections.abc import Iterator


OLD_FIELDS = ("old_string", "TargetContent", "OldText")


NEW_FIELDS = ("new_string", "ReplacementContent", "NewText")


CONTENT_FIELDS = ("content", "CodeContent", "Content")


EDIT_FIELDS = ("edits", "ReplacementChunks")


def pick(data: dict[str, object], keys: tuple[str, ...]) -> object:
    """The first field present among the harnesses' spellings."""
    return next((data[key] for key in keys if key in data), None)


def read_file(path: Path) -> str:
    """New files have empty pre-write content."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@dataclass
class Patch:
    """One file's patch and the ordered hunk under construction."""

    path: Path
    before: str
    after: str
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    cursor: int = 0

    def flush(self) -> None:
        """Apply this hunk after the previous one."""
        target = "".join(self.old)
        replacement = "".join(self.new)
        start = (self.after + "\n").index(target, self.cursor)
        self.after = (
            self.after[:start] + replacement + self.after[start + len(target) :]
        )
        self.cursor = start + len(replacement)
        self.old = []
        self.new = []

    def line(self, line: str) -> None:
        """Collect hunk lines, flushing on a patch marker."""
        if line.startswith(("*** ", "@@")):
            self.flush()
        elif line.startswith(("+", "-", " ")):
            if not line.startswith("+"):
                self.old.append(line[1:] + "\n")
            if not line.startswith("-"):
                self.new.append(line[1:] + "\n")


def patch_files(patch: str, cwd: Path) -> Iterator[tuple[Path, str, str]]:
    """Apply ordered context hunks in memory, retaining unchanged file content."""
    current: Patch | None = None
    for line in patch.splitlines():
        if current is not None:
            current.line(line)
        if line.startswith(
            ("*** Add File: ", "*** Update File: ", "*** Delete File: ")
        ):
            if current is not None:
                yield current.path, current.before, current.after
            path = cwd / line.partition(": ")[2]
            before = read_file(path)
            current = Patch(path, before, before)
        elif current is not None and line.startswith("*** Move to: "):
            current.path = cwd / line.partition(": ")[2]
    if current is not None:
        current.flush()
        yield current.path, current.before, current.after


def replacements(text: str, edits: list[dict[str, object]]) -> str:
    """Apply the harness's ordered replacement operations."""
    for edit in edits:
        old = str(pick(edit, OLD_FIELDS))
        new = str(pick(edit, NEW_FIELDS))
        if edit.get("replace_all", edit.get("ReplaceAll")):
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
    return text


def writes(data: dict[str, object]) -> Iterator[tuple[Path, str, str]]:
    """Proposed file contents from Claude, Codex and Antigravity payloads."""
    cwd = hook_cwd(data) or Path.cwd()
    tool = data.get("tool_name")
    inputs = data.get("tool_input")
    if "toolCall" in data:
        tool, inputs = agy_tool(data)
        if isinstance(inputs, str):
            inputs = json.loads(inputs)
    if not isinstance(inputs, dict):
        return
    fields: dict[str, object] = inputs
    if tool == "apply_patch":
        yield from patch_files(str(fields.get("command")), cwd)
        return
    if tool == "NotebookEdit":
        return
    name = agy_edit_path(fields)
    if not isinstance(name, str):
        return
    path = cwd / name
    before = read_file(path)
    content = pick(fields, CONTENT_FIELDS)
    if not isinstance(content, str):
        edits = pick(fields, EDIT_FIELDS)
        if isinstance(edits, list):
            operations: list[dict[str, object]] = edits
        else:
            operations = [fields]
        content = replacements(before, operations)
    yield path, before, content
