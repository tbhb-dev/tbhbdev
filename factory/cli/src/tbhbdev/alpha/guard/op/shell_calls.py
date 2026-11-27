# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Find 1Password invocations in shell commands."""

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

from tbhbdev.alpha.guard.options import flag_word
from tbhbdev.alpha.guard.shell_commands import (
    ASSIGNMENT,
    KEYWORDS,
    SHELLS,
    WRAPPERS,
    Scanner,
    program_of,
    shell_script,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


SSH_VALUES = {
    "-o",
    "-i",
    "-p",
    "-l",
    "-J",
    "-F",
    "-E",
    "-b",
    "-c",
    "-D",
    "-L",
    "-R",
    "-S",
    "-W",
    "-w",
    "-m",
    "-O",
    "-Q",
    "-B",
}


PREFIX_VALUES = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "--user", "--group"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-k", "-s", "--kill-after", "--signal"},
    "env": {"-u", "-C", "-S"},
    "xargs": {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a"},
    "exec": {"-a"},
    "watch": {"-n", "--interval"},
    "stdbuf": {"-i", "-o", "-e"},
    "setsid": set(),
    "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "doas": {"-u"},
    "flock": {"-w", "-E", "--wait", "--conflict-exit-code"},
}


def prefix(words: list[str]) -> list[str]:
    """Extend the shared wrappers with their value options and launchers."""
    i = 0
    while i < len(words):
        word = words[i]
        name = word.rpartition("/")[2]
        if word == "function":
            i += 2
            continue
        if word in KEYWORDS | {"--"} or ASSIGNMENT.match(word):
            i += 1
            continue
        special = special_prefix(words[i:])
        if special is not None:
            return prefix(special)
        if name not in WRAPPERS | PREFIX_VALUES.keys():
            break
        i += 1
        i = skip_options(words, i, PREFIX_VALUES.get(name, set()))
        if name in {"timeout", "flock"}:
            i += 1
    return words[i:]


def special_prefix(words: list[str]) -> list[str] | None:
    """Lookups do not run a program; env split strings supply shell words."""
    name = program_of(words)
    if name == "mise" and words[1:2] in (["x"], ["exec"]):
        return mise_prefix(words)
    if name == "command" and words[1:2] in (["-v"], ["-V"]):
        return []
    if name == "env" and "-S" in words[1:]:
        split = words.index("-S")
        return [
            "env",
            *words[1:split],
            *shlex.split(words[split + 1]),
            *words[split + 2 :],
        ]
    return None


def mise_prefix(words: list[str]) -> list[str] | None:
    """Only mise's explicit separator identifies its command."""
    try:
        return words[words.index("--") + 1 :]
    except ValueError:
        return None


def remote(words: list[str]) -> list[str]:
    """SSH words after its options and destination."""
    return words[
        skip_options(words, skip_options(words, 1, SSH_VALUES) + 1, SSH_VALUES) :
    ]


def skip_options(words: list[str], index: int, values: set[str]) -> int:
    """Skip flags, consuming a following value only for known options."""
    while index < len(words) and words[index].startswith("-"):
        if words[index] == "--":
            return index + 1
        option = flag_word(words[index], values)
        index += 2 if option is not None and option[1] is None else 1
    return index


@dataclass
class ShellScanner(Scanner):
    """Preserve executable shell/SSH heredocs without changing the shared scanner."""

    owners: list[list[str]] = field(default_factory=list)

    @override
    def unquoted(self, char: str) -> None:
        # Consume redirections before the shared scanner splits ampersands.
        match = re.match(
            r"(?:[0-9]*[<>](?!<)[>&]?|&>)(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+|\s+[^\s;&|]+)",
            self.text[self.pos :],
        )
        if not self.in_word and match:
            self.pos += len(match[0])
        else:
            super().unquoted(char)

    @override
    def heredoc(self) -> None:
        super().heredoc()
        self.owners.append(self.words)

    @override
    def skip_heredoc_bodies(self) -> None:
        queued = self.heredocs
        owners = self.owners
        self.owners = []
        for (delim, strip), owner in zip(queued, owners, strict=True):
            command = prefix(owner)
            execute = shell_stdin(command)
            start = self.pos
            self.heredocs = [(delim, strip)]
            super().skip_heredoc_bodies()
            body = self.text[start : self.pos].splitlines(keepends=True)
            if body and body[-1].rstrip("\n").lstrip("\t" if strip else "") == delim:
                body.pop()
            if execute:
                self.commands.extend(ShellScanner("".join(body)).run())


def shell_stdin(command: list[str]) -> bool:
    """Whether a command executes its standard input as a shell script."""
    return bool(command) and (
        (program_of(command) in SHELLS and shell_script(command[1:]) is None)
        or (program_of(command) == "ssh" and not remote(command))
    )


def shell_calls(text: str) -> Iterator[list[str]]:
    """Split shell words while preserving heredocs through recursive scripts."""
    for command in ShellScanner(text.replace("\\\n", "")).run():
        words = prefix(command)
        if not words:
            continue
        program = program_of(words)
        if program in SHELLS:
            script = shell_script(words[1:])
            if script is not None:
                yield from shell_calls(script)
        elif program == "eval":
            yield from shell_calls(" ".join(words[1:]))
        else:
            yield from direct_calls(words)


def direct_calls(words: list[str]) -> Iterator[list[str]]:
    """An op call, or shell text sent to SSH's remote shell."""
    if not words:
        return
    if program_of(words) == "find":
        yield from find_calls(words)
    elif program_of(words) == "op":
        yield words
    elif program_of(words) == "ssh":
        yield from shell_calls(" ".join(remote(words)))


def find_calls(words: list[str]) -> Iterator[list[str]]:
    """Read each find execution clause up to its terminator."""
    rest = iter(words)
    for word in rest:
        if word in {"-exec", "-execdir"}:
            command: list[str] = []
            for arg in rest:
                if arg in {";", "+"}:
                    break
                command.append(arg)
            yield from shell_calls(shlex.join(command))
