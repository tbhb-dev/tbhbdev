# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Options shared by the GitHub command guards."""

VALUE_FLAGS = {"-R", "--repo", "--hostname"}

API_VALUE_FLAGS = {
    "-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header",
    "--input", "-q", "--jq", "-t", "--template", "--cache", "-p", "--preview",
    "--hostname",
}  # fmt: skip

API_FIELD_FLAGS = {"-f", "--raw-field", "-F", "--field"}


def after_global_options(words: list[str]) -> list[str]:
    """Return the subcommand and arguments following gh's global options."""
    i = 0
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in VALUE_FLAGS else 1
    return words[i:]
