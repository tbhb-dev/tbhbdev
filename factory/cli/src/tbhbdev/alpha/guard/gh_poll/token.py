# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Decide whether shell prefixes and builtins remove the service token."""

import itertools
import re
import shlex

from tbhbdev.alpha.guard.shell_commands import (
    ASSIGNMENT,
    KEYWORDS,
    WRAPPERS,
    program_of,
)

REPEATERS = {"xargs", "parallel"}


REDIRECT = re.compile(r"\d*(?:>>?|<|>&|&>)")


TOKEN = "OP_SERVICE_" + "ACCOUNT_TOKEN"


UNSET_REASON = f"Don't unset {TOKEN}. Leave the environment as the harness gives it."


ENV_LONG = {
    "ignore-environment": "i", "null": "0", "unset": "u", "chdir": "C",
    "split-string": "S", "debug": "v", "argv0": "a", "block-signal": "",
    "default-signal": "", "ignore-signal": "", "list-signal-handling": "",
    "help": "", "version": "",
}  # fmt: skip


ENV_VALUE = {"u", "C", "S", "a", "P"}


UNEXPORT = {
    "export": ("-", "n"),
    "declare": ("+", "x"),
    "typeset": ("+", "x"),
    "local": ("+", "x"),
}


def names_token(word: str) -> bool:
    """Whether a word is the token's name: ``TOKEN``, ``$TOKEN`` or ``${TOKEN}``."""
    return word in {TOKEN, f"${TOKEN}", f"${{{TOKEN}}}"}


def split_string(text: str) -> list[str]:
    """The words of env's ``-S`` string, split as the shell would."""
    try:
        return shlex.split(text)
    except ValueError:  # an unbalanced quote
        return text.split()


def next_word(args: list[str]) -> str:
    """Take the next word of ``args`` as an option's value; "" when none is left."""
    return args.pop(0) if args else ""


def env_option(word: str, args: list[str]) -> tuple[str, str]:
    """The (letter, value) of one env option word; a separate value leaves ``args``.

    The letter is the short option's, for a long option too (``--unset`` is
    ``u``), or "" for an option that takes no value and isn't ``-i``. A long
    option may be any unique prefix. A cluster such as ``-iuNAME`` reports
    its first ``i`` or value option: ``i`` is denied whatever follows, and a
    value option takes the rest of the cluster.
    """
    if word == "-":
        return "i", ""
    if word.startswith("--"):
        name, equals, value = word[2:].partition("=")
        names = [long for long in ENV_LONG if long.startswith(name)]
        letter = ENV_LONG[names[0]] if len(names) == 1 else ""
        if letter in ENV_VALUE and not equals:
            value = next_word(args)
        return letter, value
    return cluster_option(word[1:], args)


def cluster_option(cluster: str, args: list[str]) -> tuple[str, str]:
    """The first ``i`` or value option of a cluster of short options, with its value."""
    for i, char in enumerate(cluster, 1):
        if char == "i":
            return "i", ""
        if char in ENV_VALUE:
            return char, cluster[i:] or next_word(args)
    return "", ""


def env_rest(options: list[str]) -> list[str] | None:
    """The words after env's options; None when env drops or unsets the token."""
    args = list(options)
    while args and args[0].startswith("-"):
        word = args.pop(0)
        if word == "--":
            break
        letter, value = env_option(word, args)
        if letter == "i" or (letter == "u" and names_token(value)):
            return None
        if letter == "S":
            args = split_string(value) + args
    return args


def clears_token(program: str, args: list[str]) -> bool:
    """Whether ``export -n``, ``declare +x`` and the like drop or empty the token."""
    if program not in UNEXPORT:
        return False
    sign, letter = UNEXPORT[program]
    options = list(itertools.takewhile(lambda w: w.startswith(("-", "+")), args))
    drops = any(o[0] == sign and letter in o[1:] for o in options)
    return any(
        word == f"{TOKEN}=" or (drops and names_token(word.partition("=")[0]))
        for word in args[len(options) :]
    )


def unwrap(words: list[str]) -> tuple[list[str] | None, bool]:
    """The program's words after prefixes, and whether a repeater was passed.

    The words are None when the prefixes unset the token. A repeater is
    xargs or parallel as a prefix, or inside env's ``-S`` string.

    Prefixes are the shared parser's: ``NAME=value`` words, shell keywords and its
    wrappers with their options. env's options are parsed as env does.
    ``TOKEN=`` empties the token.
    """
    rest: list[str] | None = words
    repeating = False
    while rest:
        word = rest[0]
        wrapper = program_of(rest)
        repeating = repeating or wrapper in REPEATERS
        if word == f"{TOKEN}=":
            return None, repeating
        if REDIRECT.match(word):
            rest = rest[1 if REDIRECT.fullmatch(word) is None else 2 :]
        elif word in KEYWORDS or ASSIGNMENT.match(word):
            rest = rest[1:]
        elif wrapper == "env":
            rest = env_rest(rest[1:])
        elif wrapper in WRAPPERS:
            rest = rest[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
        else:
            break
    return rest, repeating
