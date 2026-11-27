# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Parse Claude hook payloads and Bash edit reports."""

from __future__ import annotations

import re
from typing import cast

from tbhbdev.alpha.identity.records import as_dict, as_str

CLAUDE_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


SWITCH = "PostModelSwitch"


def diff_paths(text: str) -> list[str]:
    """File paths named by a unified or git diff."""
    return re.findall(r"^\+\+\+ (?:b/)?(\S.*)$", text, re.MULTILINE) + re.findall(
        r"^diff --git a/.+ b/(.+)$", text, re.MULTILINE
    )


def walk_paths(node: object) -> list[str]:
    """Paths found anywhere in a nested diff structure of dicts, lists and text."""
    if isinstance(node, str):
        return diff_paths(node)
    if isinstance(node, list):
        items = cast("list[object]", node)  # pragma: no mutate -- a runtime no-op
        return [p for item in items for p in walk_paths(item)]
    fields = as_dict(node)
    if fields is None:
        return []
    out = [
        path
        for k in ("filePath", "file_path", "path")
        if (path := as_str(fields.get(k)))
    ]
    return out + [p for value in fields.values() for p in walk_paths(value)]


def edit_paths_from_bash(resp: object) -> list[str]:
    """Paths from ``tool_response.bashEditDiff`` (undocumented shape).

    Accepts a dict, a list or diff text.
    """
    diff = (as_dict(resp) or {}).get("bashEditDiff")
    if diff is None:
        return []
    return [p for p in dict.fromkeys(walk_paths(diff)) if p != "/dev/null"]
