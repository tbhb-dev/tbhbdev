# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Decide whether a shell command adds a sign-off or co-author trailer."""

import re
from typing import TYPE_CHECKING

from tbhbdev.alpha.guard.git_command import subcommand
from tbhbdev.alpha.guard.shell_commands import program_of, simple_commands

if TYPE_CHECKING:
    from tbhbdev.alpha.repo import Json


TRAILER = re.compile(r"(?i)(signed-off-by|co-authored-by)\s*:")


VALUE_FLAGS = frozenset(
    {
        "--message",
        "--file",
        "--template",
        "--reuse-message",
        "--reedit-message",
        "--author",
        "--date",
        "--cleanup",
        "--trailer",
        "--fixup",
        "--squash",
        "--pathspec-from-file",
    }
)


SHORT_VALUES = frozenset("mFCctU")


SHORT_OPTIONAL_VALUES = frozenset("Su")


def short_signoff(word: str) -> tuple[bool, bool]:
    """Whether WORD signs off, and whether its next argument is a value."""
    if not word.startswith("-") or word.startswith("--"):
        return False, False
    for index, flag in enumerate(word[1:], start=1):
        if flag == "s":
            return True, False
        if flag in SHORT_VALUES:
            return False, index == len(word) - 1
        if flag in SHORT_OPTIONAL_VALUES:
            return False, False
    return False, False


def signs_off(arguments: list[str]) -> bool:
    """Whether a `git commit`'s ARGUMENTS add a sign-off or co-author trailer."""
    if TRAILER.search(" ".join(arguments)):
        return True
    skip = False  # pragma: no mutate -- None also means no value to skip
    for word in arguments:
        if skip:
            skip = False  # pragma: no mutate -- None also means no value to skip
            continue
        if word == "--signoff":
            return True
        signed, skip = short_signoff(word)
        if signed:
            return True
        skip = skip or word in VALUE_FLAGS
    return False


def commit_trailer_reason(command: str) -> str | None:
    """Why COMMAND, a `git commit` that adds a sign-off trailer, is blocked."""
    for _, words in simple_commands(command):
        if program_of(words) != "git":
            continue
        index, name = subcommand(words)
        if name == "commit" and signs_off(words[index + 1 :]):
            return (
                "state-guard: agent commits carry no Signed-off-by or "
                "Co-authored-by trailer and no -s or --signoff. Remove it."
            )
    return None


def pre_tool_reason(data: Json) -> str | None:
    """Why the PreToolUse call DATA describes is blocked, or None."""
    tool = data.get("tool_name")
    raw = data.get("tool_input")
    tool_input: Json = raw if isinstance(raw, dict) else {}
    if tool == "Bash":
        return commit_trailer_reason(str(tool_input.get("command", "")))
    return None
