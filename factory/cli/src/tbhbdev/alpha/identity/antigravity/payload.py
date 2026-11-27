# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read Antigravity edit tool names and paths from hook payloads."""

from __future__ import annotations

import json

from tbhbdev.alpha.identity.records import Json, as_dict, as_str

AGY_EDIT_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}


AGY_PATH_KEYS = (
    "Path",
    "TargetFile",
    "AbsolutePath",
    "target_file",
    "file_path",
    "filePath",
    "path",
)


def agy_tool(data: Json) -> tuple[object, object]:
    """(tool name, args) from agy hook stdin."""
    call = as_dict(data.get("toolCall")) or {}
    return (
        call.get("name") or data.get("toolName") or data.get("tool_name"),
        call.get("args") or data.get("toolInput"),
    )


def agy_edit_path(args: object) -> str | None:
    """The edited file's path from an agy tool call's args (dict or JSON text)."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    fields = as_dict(args)
    if fields is None:
        return None
    return next((p for key in AGY_PATH_KEYS if (p := as_str(fields.get(key)))), None)
