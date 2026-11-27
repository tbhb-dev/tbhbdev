"""Quoting and indentation for the generated token scripts."""

lazy from collections.abc import Sequence


def quote(text: str) -> str:
    """TEXT in single quotes, whatever it holds."""
    return "'" + text.replace("'", "'\\''") + "'"


def indent(lines: Sequence[str], width: int) -> list[str]:
    """LINES, each one indented by WIDTH spaces."""
    return [" " * width + line for line in lines]
