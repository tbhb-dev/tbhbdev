# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read flag values and positional arguments from command words."""


def flag_word(word: str, value_flags: set[str]) -> tuple[str, str | None] | None:
    """The value flag a flag word sets, with its attached value, or None.

    ``--flag=v`` and ``-Xv`` attach the value. ``--flag`` and ``-X``, also at
    the end of a cluster such as ``-aX``, take the next word (None here).
    """
    if word.startswith("--"):
        name, equals, value = word.partition("=")
        return (name, value if equals else None) if name in value_flags else None
    for index, char in enumerate(word[1:], 2):
        if f"-{char}" in value_flags:
            return f"-{char}", word[index:] or None
    return None


def flag_values(words: list[str], flags: set[str], value_flags: set[str]) -> list[str]:
    """The values of ``flags``, read from flag words only, not other values."""
    values: list[str] = []
    rest = iter(words)
    for word in rest:
        found = flag_word(word, value_flags) if word.startswith("-") else None
        if found is None:
            continue
        name, value = found
        value = next(rest, None) if value is None else value
        if name in flags and value is not None:
            values.append(value)
    return values


def takes_value(word: str, value_flags: set[str]) -> bool:
    """Whether a flag word's value is the next word: ``-b``, or ``-ab`` ending in it."""
    found = flag_word(word, value_flags)
    return found is not None and found[1] is None


def split_args(words: list[str], value_flags: set[str]) -> tuple[list[str], list[str]]:
    """(flag words, positional words); a flag's separate value is in neither."""
    flags: list[str] = []
    found: list[str] = []
    rest = iter(words)
    for word in rest:
        if word.startswith("-"):
            flags.append(word)
            if takes_value(word, value_flags):
                next(rest, None)
        else:
            found.append(word)
    return flags, found
