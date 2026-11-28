"""Read the host command's arguments and shape the build record."""

from __future__ import annotations

import json

DIGEST_OPTION = "--cli-sha256"
# The dispatcher's options that take a value; the rest take none.
_VALUED = ("--host", "--prompt")


def split_digest(argv: list[str]) -> tuple[str | None, list[str]]:
    """The `--cli-sha256` value, and ARGV without that option and its value."""
    digest = None
    rest: list[str] = []
    pending = iter(argv)
    for argument in pending:
        if argument == DIGEST_OPTION:
            digest = next(pending, None)
        elif argument.startswith(f"{DIGEST_OPTION}="):
            digest = argument.partition("=")[2]
        else:
            rest.append(argument)
    return digest or None, rest


def run_name(argv: list[str]) -> str | None:
    """The first positional argument, which the dispatcher reads as the run name."""
    pending = iter(argv)
    for argument in pending:
        if argument in _VALUED:
            next(pending, None)
        elif not argument.startswith("-"):
            return argument
    return None


def record(commit: str, module: str, sha256: str) -> str:
    return json.dumps({"commit": commit, "module": module, "sha256": sha256}) + "\n"
