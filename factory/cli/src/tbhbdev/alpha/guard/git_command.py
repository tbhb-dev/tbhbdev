# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Find the Git subcommand after its global options."""

GLOBAL_VALUE_OPTIONS = {
    "-c",
    "-C",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
}


def subcommand(words: list[str]) -> tuple[int, str]:
    """The index and name of the git subcommand, after git's own options."""
    at = 1
    while at < len(words):
        word = words[at]
        if word in GLOBAL_VALUE_OPTIONS:
            at += 2
        elif word.startswith("-"):
            at += 1
        else:
            return at, word
    return len(words), ""
