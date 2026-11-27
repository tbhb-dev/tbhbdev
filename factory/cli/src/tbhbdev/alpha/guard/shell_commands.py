# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse shell commands and their option words without running them."""

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, override

if TYPE_CHECKING:
    from collections.abc import Iterator


SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}


WRAPPERS = {"env", "command", "exec", "sudo", "nohup", "time", "builtin", "xargs"}


KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "{", "}"}


ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")


SEPARATORS = ";&|"


@dataclass
class Scanner:
    """Split shell text into simple commands, each a list of unquoted words."""

    text: str
    pos: int = 0
    quote: str = ""
    word: list[str] = field(default_factory=list)
    in_word: bool = False
    words: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    # One (kind, quote to restore) per open `$(`, `(` or backtick.
    nesting: list[tuple[str, str]] = field(default_factory=list)
    heredocs: list[tuple[str, bool]] = field(default_factory=list)

    def end_word(self) -> None:
        """Close the word being read, if any."""
        if self.in_word:
            self.words.append("".join(self.word))
        self.word = []
        self.in_word = False

    def end_command(self) -> None:
        """Close the word and the simple command being read."""
        self.end_word()
        if self.words:
            self.commands.append(self.words)
        self.words = []

    def add(self, char: str) -> None:
        """Append a literal character to the current word."""
        self.word.append(char)
        self.in_word = True

    def open_nesting(self, kind: str, width: int) -> None:
        """Enter ``$(``, ``(`` or a backtick: a new command, unquoted."""
        self.end_command()
        self.nesting.append((kind, self.quote))
        self.quote = ""
        self.pos += width

    def close_nesting(self) -> None:
        """Leave a ``)`` or a closing backtick, restoring the outer quote."""
        self.end_command()
        if self.nesting:
            self.quote = self.nesting.pop()[1]
        self.pos += 1

    def backtick(self) -> None:
        """A backtick opens a substitution, or closes the one that is open."""
        if self.nesting and self.nesting[-1][0] == "`":
            self.close_nesting()
        else:
            self.open_nesting("`", 1)

    def heredoc(self) -> None:
        """Read the delimiter after ``<<`` and queue the body to be skipped."""
        self.pos += 2
        strip = self.text.startswith("-", self.pos)
        self.pos += strip
        while self.text[self.pos : self.pos + 1] in {" ", "\t"}:
            self.pos += 1
        delim: list[str] = []
        quote = ""
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if quote:
                if char == quote:
                    quote = ""
                else:
                    delim.append(char)
            elif char in "'\"":
                quote = char
            elif char.isspace() or char in SEPARATORS + "()<>":
                break
            else:
                delim.append(char)
            self.pos += 1
        self.heredocs.append(("".join(delim), strip))

    def skip_heredoc_bodies(self) -> None:
        """After a newline, drop each queued heredoc body up to its delimiter."""
        for delim, strip in self.heredocs:
            while self.pos < len(self.text):
                end = self.text.find("\n", self.pos)
                end = len(self.text) if end < 0 else end
                line = self.text[self.pos : end]
                self.pos = min(end + 1, len(self.text))
                if (line.lstrip("\t") if strip else line) == delim:
                    break
        self.heredocs = []

    def quoted(self, char: str) -> None:
        """One character inside single or double quotes."""
        if char == self.quote:
            self.quote = ""
        elif self.quote == '"' and char == "\\":
            self.add(self.text[self.pos + 1 : self.pos + 2])
            self.pos += 1
        elif self.quote == '"' and self.text.startswith("$(", self.pos):
            self.open_nesting("$", 2)
            return
        elif self.quote == '"' and char == "`":
            self.backtick()
            return
        else:
            self.add(char)
        self.pos += 1

    def unquoted(self, char: str) -> None:
        """One character outside quotes."""
        if self.text.startswith("$(", self.pos):
            self.open_nesting("$", 2)
        elif self.text.startswith("<<<", self.pos):
            self.end_word()
            self.pos += 3
        elif self.text.startswith("<<", self.pos):
            self.end_word()
            self.heredoc()
        elif char == "`":
            self.backtick()
        elif char == "(":
            self.open_nesting("(", 1)
        elif char == ")":
            self.close_nesting()
        else:
            self.plain(char)

    def plain(self, char: str) -> None:
        """Quotes, escapes, separators, comments and ordinary characters."""
        self.pos += 1
        if char == "\\":
            if self.text[self.pos : self.pos + 1] != "\n":
                self.add(self.text[self.pos : self.pos + 1])
            self.pos += 1
        elif char in "'\"":
            self.quote, self.in_word = char, True
        elif char == "\n":
            self.end_command()
            self.skip_heredoc_bodies()
        elif char in SEPARATORS:
            self.end_command()
        elif char.isspace():
            self.end_word()
        elif char == "#" and not self.in_word:
            end = self.text.find("\n", self.pos)
            self.pos = len(self.text) if end < 0 else end
        else:
            self.add(char)

    def run(self) -> list[list[str]]:
        """Scan the whole text."""
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if self.quote:
                self.quoted(char)
            else:
                self.unquoted(char)
        self.end_command()
        return self.commands


def skip_prefix(words: list[str]) -> list[str]:
    """The command's words after env assignments, keywords and wrappers."""
    i = 0
    while i < len(words):
        word = words[i]
        program = word.rpartition("/")[2]
        if word in KEYWORDS or ASSIGNMENT.match(word):
            i += 1
        elif program in WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                value = program == "env" and words[i] in {
                    "-u",
                    "--unset",
                    "-C",
                    "--chdir",
                }
                i += 2 if value else 1
        else:
            break
    return words[i:]


def shell_script(rest: list[str]) -> str | None:
    """The script word of ``-c`` among a shell's options, if it has one."""
    for i, word in enumerate(rest):
        if not word.startswith("-"):
            return None
        if not word.startswith("--") and "c" in word:
            return rest[i + 1] if i + 1 < len(rest) else None
    return None


def program_of(words: list[str]) -> str:
    """A command's program name, without its directory."""
    return words[0].rpartition("/")[2]


class Simple(NamedTuple):
    """A simple command: assignments and wrappers, then the program's words."""

    prefix: list[str]
    words: list[str]


def simple_commands(text: str) -> Iterator[Simple]:
    """Each simple command the shell would run, split from its prefix words.

    ``bash -c SCRIPT`` and ``eval`` are scanned in their place, under the
    shell's own prefix.
    """
    for command in Scanner(text).run():
        words = skip_prefix(command)
        if not words:
            continue
        prefix = command[: len(command) - len(words)]
        program = program_of(words)
        if program in SHELLS:
            script = shell_script(words[1:])
            inner = simple_commands(script) if script is not None else iter(())
        elif program == "eval":
            inner = simple_commands(" ".join(words[1:]))
        else:
            yield Simple(prefix, words)
            continue
        for found in inner:
            yield Simple(prefix + found.prefix, found.words)


SSH_VALUE_FLAGS = {
    "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
    "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}  # fmt: skip


@dataclass
class BodyScanner(Scanner):
    """Keep the heredoc text read after each command.

    ``bodies`` maps a command's index in the scan to the heredoc text skipped
    at the end of its line, delimiter lines included.
    """

    bodies: dict[int, str] = field(default_factory=dict)
    pending: list[int] = field(default_factory=list)

    @override
    def heredoc(self) -> None:
        """Note the command a heredoc belongs to, then read its delimiter."""
        self.pending.append(len(self.commands))
        super().heredoc()

    @override
    def skip_heredoc_bodies(self) -> None:
        """Skip the queued bodies, keeping their text for their commands."""
        start = self.pos
        super().skip_heredoc_bodies()
        for index in self.pending:
            self.bodies[index] = self.text[start : self.pos]
        self.pending = []


def remote_command(args: list[str]) -> str | None:
    """The remote command of ssh after its options and host; None without one."""
    rest = iter(args)
    for word in rest:
        if word in SSH_VALUE_FLAGS:
            next(rest, None)
        elif not word.startswith("-"):
            return " ".join(rest) or None
    return None


def inner_script(words: list[str], body: str | None) -> str | None:
    """The script a shell, eval or ssh runs, or None for any other program."""
    program, args = program_of(words), words[1:]
    if program in SHELLS:
        script = shell_script(args)
        reads_stdin = all(word.startswith("-") for word in args)
        return body if script is None and reads_stdin else script
    if program == "eval":
        return " ".join(args)
    if program == "ssh":
        script = remote_command(args)
        return body if script is None else script
    return None
