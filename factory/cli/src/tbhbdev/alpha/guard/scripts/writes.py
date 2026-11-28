# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Collect proposed file writes from shell commands and tool payloads."""

import json
import os
import posixpath
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, override

from tbhbdev.alpha.guard.options import flag_values, split_args
from tbhbdev.alpha.guard.payload import command_of
from tbhbdev.alpha.guard.scripts.paths import Place, normalize
from tbhbdev.alpha.guard.shell_commands import (
    BodyScanner,
    inner_script,
    program_of,
    skip_prefix,
)
from tbhbdev.alpha.identity.antigravity.payload import agy_edit_path, agy_tool

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tbhbdev.alpha.guard.scripts.paths import Write
    from tbhbdev.alpha.repo import Json


FD = re.compile(r"[0-9]+")


UNRESOLVED = re.compile(r"\$|^[-~]")


DUP = re.compile(r"[0-9]+-?|-")


COPIERS = {"cp", "mv", "install", "ln"}


COPY_VALUE_FLAGS = {
    "-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner",
    "-g", "--group", "--strip-program",
}  # fmt: skip


TARGET_DIRECTORY = {"-t", "--target-directory"}


CONTENT_FIELDS = ("content", "CodeContent", "Content")


PATCH_FIELDS = ("patch", "input", "command")


FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write_file"}


class Redirect(str):
    """A redirect operator in a command's words; the word after it is the target."""

    __slots__ = ()


@dataclass
class RedirectScanner(BodyScanner):
    """The heredoc-keeping scanner, reading ``&>``, ``>|`` and ``N>`` as redirects.

    The base scanner splits a command at ``&`` and ``|``, which would lose the
    target of ``&> f`` and ``>| f``. It also keeps, for each command, how many
    ``(``, ``$(`` and backticks enclose it (``depths``), and which commands
    have a substitution among their words (``substituted``).
    """

    depths: list[int] = field(default_factory=list)
    substituted: set[int] = field(default_factory=set)

    @override
    def end_command(self) -> None:
        """Close the command, noting how deeply it is nested."""
        count = len(self.commands)
        super().end_command()
        if len(self.commands) > count:
            self.depths.append(len(self.nesting))

    @override
    def open_nesting(self, kind: str, width: int) -> None:
        """Note a substitution in the words of the command read so far."""
        if kind != "(" and self.words:
            self.substituted.add(len(self.commands))
        super().open_nesting(kind, width)

    @override
    def plain(self, char: str) -> None:
        """Read a redirect operator as one word, before the separator rules."""
        if char == ">" or (char == "&" and self.text.startswith(">", self.pos + 1)):
            self.redirect()
        else:
            super().plain(char)

    def redirect(self) -> None:
        """Read the operator and drop a file descriptor number before it."""
        end = self.text.index(">", self.pos) + 1
        if self.text[end : end + 1] in {">", "|", "&"}:
            end += 1
        descriptor = FD.fullmatch("".join(self.word)) is not None
        self.end_word()
        if descriptor:
            self.words.pop()
        self.words.append(Redirect(self.text[self.pos : end]))
        self.pos = end


def read_first_line(path: str, *, python: bool) -> str | None:
    """Read a Python first line, or two bytes to detect a shebang."""
    try:
        if not Path(path).is_file():
            return None
        with Path(path).open("rb") as file:
            return (file.readline() if python else file.read(2)).decode(
                errors="replace"
            )
    except OSError:
        return None


def first_str(fields: Json, keys: tuple[str, ...]) -> str:
    """The first of ``keys`` that holds text in ``fields``, or ""."""
    return next((v for key in keys if isinstance(v := fields.get(key), str)), "")


def tool_call(data: Json) -> tuple[object, Json]:
    """(tool name, input fields) from Claude, Codex or Antigravity hook stdin."""
    if isinstance(data.get("toolCall"), dict):
        name, inputs = agy_tool(data)
        if isinstance(inputs, str):  # Antigravity may send the arguments as JSON text
            try:
                inputs = json.loads(inputs)
            except ValueError:
                return name, {}
    else:
        name, inputs = data.get("tool_name"), data.get("tool_input")
    return name, inputs if isinstance(inputs, dict) else {}


def redirects(command: list[str]) -> tuple[list[str], list[str]]:
    """(redirect targets, the other words) of a simple command.

    A target that duplicates a file descriptor (``>&2``) is not a file.
    """
    targets: list[str] = []
    words: list[str] = []
    rest = iter(command)
    for word in rest:
        if not isinstance(word, Redirect):
            words.append(word)
        elif (target := next(rest, None)) is not None and not (
            word.endswith("&") and DUP.fullmatch(target)
        ):
            targets.append(target)
    return targets, words


def copy_writes(program: str, args: list[str], place: Place) -> Iterator[Write]:
    """The files ``cp``, ``mv``, ``install`` or ``ln`` writes."""
    flags, operands = split_args(args, COPY_VALUE_FLAGS)
    if program == "install" and {"-d", "--directory"} & set(flags):
        return
    directory = flag_values(args, TARGET_DIRECTORY, COPY_VALUE_FLAGS)
    if directory:
        dest, sources = directory[-1], operands
    elif program == "ln" and len(operands) == 1:
        dest, sources = "./", operands
    elif len(operands) >= 2:  # noqa: PLR2004 -- a source and a destination
        dest, sources = operands[-1], operands[:-1]
    else:
        return
    into = (
        bool(directory)
        or len(sources) > 1
        or dest.endswith("/")
        or os.path.isdir(place.resolve(dest))  # noqa: PTH112 -- the string is the path
    )
    for source in sources:
        target = f"{dest}/{posixpath.basename(source)}" if into else dest
        first_line = read_first_line(
            place.resolve(source), python=target.endswith(".py")
        )
        script = source.endswith((".sh", ".py")) or bool(
            first_line and first_line.startswith("#!")
        )
        yield place.write(target, content=script, first_line=first_line)


def command_writes(
    command: list[str], body: str | None, place: Place
) -> Iterator[Write]:
    """The writes of one simple command, and of the script it runs."""
    shebang = body is not None and body.startswith("#!")
    targets, words = redirects(command)
    for target in targets:
        yield place.write(target, content=shebang, first_line=body)
    rest = skip_prefix(words)
    if not rest:
        return
    program, args = program_of(rest), rest[1:]
    if program == "tee":
        for operand in (word for word in args if not word.startswith("-")):
            yield place.write(operand, content=shebang, first_line=body)
    elif program in COPIERS:
        yield from copy_writes(program, args, place)
    # ssh runs its command on another host, whose files this host can't judge.
    script = None if program == "ssh" else inner_script(rest, body)
    if script is not None:
        yield from bash_writes(script, place)


def cd_dir(
    command: list[str], place: Place, start: str, *, substituted: bool
) -> str | None:
    """The directory a ``cd`` command moves to; None when it isn't a ``cd``.

    ``cd`` alone goes home. A target that stays unexpanded, a flag, more than
    one operand or a substitution leaves ``start``, the directory the text began in.
    """
    words = skip_prefix(redirects(command)[1])
    if not words or words[0] != "cd":
        return None
    args = words[1:]
    expanded = place.expand(args[0] if args else "~")
    if substituted or len(args) > 1 or UNRESOLVED.search(expanded):
        return start
    return normalize(expanded, place.cwd)


def bash_writes(text: str, place: Place) -> Iterator[Write]:
    """The writes in a shell command, each judged in the directory a ``cd`` left."""
    scanner = RedirectScanner(text)
    commands = scanner.run()
    dirs = [place.cwd]  # the current directory at each nesting depth
    for index, command in enumerate(commands):
        depth = scanner.depths[index]
        del dirs[depth + 1 :]
        while len(dirs) <= depth:
            dirs.append(dirs[-1])
        here = replace(place, cwd=dirs[-1])
        yield from command_writes(command, scanner.bodies.get(index), here)
        moved = cd_dir(
            command, here, place.cwd, substituted=index in scanner.substituted
        )
        dirs[-1] = moved or dirs[-1]


def patch_writes(patch: str, place: Place) -> list[Write]:
    """The paths an apply_patch patch adds, updates or moves to."""
    lines = patch.split("\n")
    writes: list[Write] = []
    for number, line in enumerate(lines):
        kind, _, name = line.partition(": ")
        if kind == "*** Add File":
            shebang = any(x.startswith("+#!") for x in lines[number + 1 : number + 2])
            first_line = lines[number + 1] if number + 1 < len(lines) else ""
            writes.append(
                place.write(
                    name, content=shebang, first_line=first_line.removeprefix("+")
                )
            )
        elif kind == "*** Update File":
            writes.append(place.write(name))
        elif kind == "*** Move to" and writes:
            writes.append(place.write(name, content=writes[-1].script))
    return writes


def file_writes(inputs: Json, place: Place) -> list[Write]:
    """The file a Write, Edit, MultiEdit, NotebookEdit or write_file call names."""
    raw = first_str(inputs, ("notebook_path",)) or agy_edit_path(inputs)
    if not raw:
        return []
    text = next(
        (v for key in CONTENT_FIELDS if isinstance(v := inputs.get(key), str)), None
    )
    content = text is not None and text.startswith("#!")
    return [place.write(raw, content=content, first_line=text)]


def targets(data: Json, place: Place) -> list[Write]:
    """Every file a tool call writes."""
    tool, inputs = tool_call(data)
    if tool == "apply_patch":
        return patch_writes(first_str(inputs, PATCH_FIELDS), place)
    text = command_of(data)[0]
    if text:
        return list(bash_writes(text, place))
    return file_writes(inputs, place) if tool in FILE_TOOLS else []
