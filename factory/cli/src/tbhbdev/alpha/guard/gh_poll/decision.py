# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Decide which GitHub polling and token changes the hook refuses."""

import re
import shlex
from urllib.parse import parse_qs, urlsplit

from tbhbdev.alpha.guard.gh_options import (
    API_FIELD_FLAGS,
    API_VALUE_FLAGS,
    after_global_options,
)
from tbhbdev.alpha.guard.gh_poll.token import (
    REPEATERS,
    UNSET_REASON,
    clears_token,
    names_token,
    split_string,
    unwrap,
)
from tbhbdev.alpha.guard.options import flag_values, split_args
from tbhbdev.alpha.guard.shell_commands import (
    KEYWORDS,
    BodyScanner,
    inner_script,
    program_of,
)

SELF_MATCHING_PGREP = "pgrep -f"


POLL_REASON = (
    "Don't poll GitHub. Subscribe to the awrel event instead, for example "
    "check_run conclusion events for the repository through awrel, and act when "
    "it arrives. Bulk and polling GitHub reads are blocked."
)


GH_RUN = re.compile(r"\bgh\b.*\brun\b", re.DOTALL)


REPEAT_REASON = (
    "Don't run a CI listing through xargs or parallel; each repeat is a separate "
    "call. Ask the coordinator."
)


LIST_PATH = re.compile(
    r"(?:.*/)?repos/[^/]+/[^/]+/(?:actions/runs|actions/workflows/[^/]+/runs"
    r"|actions/jobs|check-runs|check-suites)"
)


RUNS_PATH = re.compile(r"(?:.*/)?repos/[^/]+/[^/]+/actions/runs")


SHA = re.compile(r"[0-9a-fA-F]{40}")


LOOPS = {"while", "until", "for"}


def writes(args: list[str], flags: list[str]) -> bool:
    """Whether ``gh api`` arguments send a write: a method other than GET or fields."""
    methods = flag_values(args, {"-X", "--method"}, API_VALUE_FLAGS)
    if methods and not methods[-1]:
        methods = list[str]()  # pragma: no mutate  # None is falsy too
    if methods:
        return methods[-1].upper() not in {"GET", "HEAD"}
    return bool(
        flag_values(args, API_FIELD_FLAGS, API_VALUE_FLAGS) or "--input" in flags
    )


def bulk_api(args: list[str], *, repeated: bool = False) -> bool:
    """Whether ``gh api`` arguments page, or read a list endpoint.

    ``per_page=1`` allows a single read, never a repeated one.
    """
    flags, endpoints = split_args(args, API_VALUE_FLAGS)
    if any(flag.partition("=")[0] == "--paginate" for flag in flags):
        return True
    if writes(args, flags):
        return False
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        path = parsed.path.rstrip("/")
        if not LIST_PATH.fullmatch(path):
            continue
        heads = parse_qs(parsed.query).get("head_sha", [])
        one_sha = len(heads) == 1 and SHA.fullmatch(heads[0])
        one_page = not repeated and parse_qs(parsed.query).get("per_page") == ["1"]
        if not (one_page or (RUNS_PATH.fullmatch(path) and one_sha)):
            return True
    return False


def limits_to_one(words: list[str]) -> bool:
    """Whether ``gh run list`` words ask for one run: ``--limit 1`` or ``-L 1``.

    The last limit wins, as it does in gh, and ``-L1`` counts.
    """
    limit = None
    for at, word in enumerate(words):
        flag, equals, value = word.partition("=")
        if flag in ("--limit", "-L"):
            limit = value if equals else "".join(words[at + 1 : at + 2])
        elif word.startswith("-L") and not word.startswith("--"):
            limit = word[2:]
    return limit == "1"


def run_poll(rest: list[str], *, repeated: bool) -> bool:
    """Whether ``gh run`` words poll."""
    verb, *tail = after_global_options(rest[1:]) or [""]
    if verb == "watch":
        return True
    if verb in {"list", "ls"}:
        return repeated or not limits_to_one(tail)
    return False


def github_poll(args: list[str], *, repeated: bool = False) -> bool:
    """Whether the words after ``gh``, past its global flags, poll GitHub."""
    rest = after_global_options(args)
    if rest[:1] == ["run"]:
        return run_poll(rest, repeated=repeated)
    if rest[:2] == ["pr", "checks"]:
        return any(w.partition("=")[0] == "--watch" for w in rest[2:])
    return rest[:1] in (["api"], ["query"]) and bulk_api(rest[1:], repeated=repeated)


def opens_loop(command: list[str]) -> bool:
    """Whether a simple command starts a while, until or for loop."""
    for word in command:
        if word in LOOPS:
            return True
        if word not in KEYWORDS:
            return False
    return False


def flattened(words: list[str], depth: int = 4) -> list[str]:
    """The words with each quoted or spaced word split as the shell would.

    ``env -S 'xargs gh run'``, ``sh -c 'gh r""un'`` and ``x."count"`` come out
    as the words they run or name. Splitting goes ``depth`` levels deep.
    """
    out: list[str] = []
    for word in words:
        parts = split_string(word)
        if parts == [word] or depth == 0:
            out.append(word)
        else:
            out.extend(flattened(parts, depth - 1))
    return out


def repeats(command: list[str]) -> bool:
    """Whether xargs or parallel would run a ``gh run`` command more than once.

    The listing words can arrive from stdin or ``-a``, so ``gh run`` alone counts.
    The words are read after unquoting, including an option's value and env's
    ``-S`` string.
    """
    words = flattened(command)
    names = {word.rpartition("/")[2] for word in words}
    return bool(REPEATERS & names) and bool(GH_RUN.search(" ".join(words)))


def repeated_scan(args: list[str]) -> tuple[str | None, set[str]]:
    """Scan what a repeater may run: each suffix of its words, as repeated shell text.

    The repeater's own options and the program they lead to aren't told
    apart, so every suffix is read as a command; a suffix that isn't one
    finds nothing.
    """
    programs: set[str] = set()
    for i in range(len(args)):
        message, seen = inspect(shlex.join(args[i:]), repeated=True)
        if message:
            return message, programs
        programs |= seen
    return None, programs


def denial(
    command: list[str], body: str | None, *, repeated: bool = False
) -> tuple[str | None, set[str]]:
    """One simple command's deny reason, and the programs it runs.

    ``repeated`` is set when xargs or parallel runs the command more than once,
    so single-read exceptions do not apply.
    """
    words, passed = unwrap(command)
    repeated = repeated or passed
    if words is None:
        return UNSET_REASON, set()
    if not words:
        return None, set()
    program = program_of(words)
    unset = program == "unset" and any(names_token(word) for word in words[1:])
    if unset or clears_token(program, words[1:]):
        return UNSET_REASON, set()
    if program == "gh" and github_poll(words[1:], repeated=repeated):
        return POLL_REASON, set()
    script = inner_script(words, body)
    if program in REPEATERS:
        message, inner = repeated_scan(words[1:])
    elif script is None:
        return None, {SELF_MATCHING_PGREP if self_matching(program, words) else program}
    else:
        message, inner = inspect(script, repeated=repeated)
    return message, {program, *inner}


def self_matching(program: str, words: list[str]) -> bool:
    """Whether a command is ``pgrep -f`` with a pattern that matches its own shell.

    A bracket in the pattern, as in ``pgrep -f '[f]etch.py'``, keeps the pattern
    from matching its own command line, so that form is not self-matching.
    """
    if program != "pgrep":
        return False
    args = words[1:]
    full = any(
        word == "--full" or (word[:1] == "-" and word[1:2] != "-" and "f" in word)
        for word in args
    )
    return full and not any("[" in word for word in args)


def inspect(text: str, *, repeated: bool = False) -> tuple[str | None, set[str]]:
    """The deny reason for a shell command, or None, and the programs it runs."""
    return scan_commands(text, repeated=repeated)


def checked(
    command: list[str], body: str | None, *, repeated: bool = False
) -> tuple[str | None, set[str]]:
    """One simple command's deny reason, refusing a repeated listing first."""
    command = [command[0].removeprefix("$"), *command[1:]] if command else command
    if repeats(command):
        return REPEAT_REASON, set()
    return denial(command, body, repeated=repeated)


def scan_commands(text: str, *, repeated: bool = False) -> tuple[str | None, set[str]]:
    """Scan each command of a shell text for a denial.

    Each open loop collects the programs run from its keyword to its
    ``done``; an outer loop also collects its inner loops' programs.
    """
    loops: list[set[str]] = []
    programs: set[str] = set()
    scanner = BodyScanner(text)
    for index, command in enumerate(scanner.run()):
        if opens_loop(command):
            loops.append(set())
        message, seen = checked(command, scanner.bodies.get(index), repeated=repeated)
        if message:
            return message, programs
        programs |= seen
        for loop in loops:
            loop.update(seen)
            if "gh" in loop or {"sleep", SELF_MATCHING_PGREP} <= loop:
                return POLL_REASON, programs
        if seen == {"done"} and loops:
            loops.pop()
    return None, programs
