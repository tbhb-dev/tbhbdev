# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Find 1Password invocations in proposed file contents."""

import ast
import re
from typing import TYPE_CHECKING

import yaml

from tbhbdev.alpha.guard.op.shell_calls import shell_calls

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


YAML_COMMANDS = {
    "run",
    "shell",
    "command",
    "cmd",
    "ansible.builtin.shell",
    "ansible.builtin.command",
}


FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


SHEBANG = re.compile(r"^#!.*\b(?:sh|bash|zsh)\b")


SHELL_FENCES = {"sh", "bash", "shell", "zsh", "console", "sh-session", "shellscript"}


def literal(value: ast.AST) -> object:
    """A string literal's value, or None for computed expressions."""
    return value.value if isinstance(value, ast.Constant) else None


class Argv(list[str]):
    """Literal argv: a flag may take a value computed by the generating code."""


def argv_call(words: list[object]) -> Argv | None:
    """Recognize literal op argv while retaining computed argument text."""
    if (
        not words
        or not isinstance(words[0], str)
        or words[0].rpartition("/")[2] != "op"
    ):
        return None
    return Argv(str(word) for word in words)


def python_calls(text: str) -> Iterator[list[str]]:
    """Inspect command argv literals and supported shell=True/system calls."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            call = argv_call([argv_word(e) for e in node.elts])
            if call:
                yield call
        script = python_shell(node)
        if script is not None:
            yield from shell_calls(script)


def argv_word(node: ast.AST) -> object:
    """Keep expression text in normalized argv for before/after comparison."""
    return literal(node) if isinstance(node, ast.Constant) else ast.unparse(node)


def python_shell(node: ast.AST) -> str | None:
    """A literal shell script in one of the supported Python call sites."""
    if (
        not isinstance(node, ast.Call)
        or not node.args
        or not isinstance(node.func, ast.Attribute)
    ):
        return None
    owner = node.func.value
    if not isinstance(owner, ast.Name):
        return None
    system = owner.id == "os" and node.func.attr in {"system", "popen"}
    subprocess = owner.id == "subprocess" and any(
        k.arg == "shell" and literal(k.value) is True for k in node.keywords
    )
    script = literal(node.args[0])
    return script if (system or subprocess) and isinstance(script, str) else None


def yaml_calls(value: object) -> Iterator[list[str]]:
    """Visit YAML command fields and argv lists, including nested workflows."""
    if isinstance(value, dict):
        fields: dict[object, object] = value
        for key, child in fields.items():
            yield from yaml_field(key, child)
    elif isinstance(value, list):
        children: list[object] = value
        for child in children:
            yield from yaml_calls(child)


def yaml_field(key: object, child: object) -> Iterator[list[str]]:
    """Interpret a YAML command field, or visit its children."""
    if key in YAML_COMMANDS and isinstance(child, str):
        yield from shell_calls(child)
    elif key in {"argv", "command", "ansible.builtin.command"} and isinstance(
        child, list
    ):
        call = argv_call(child)
        if call:
            yield call
    elif key in {"script", "before_script", "after_script"} and isinstance(child, list):
        scripts: list[object] = child
        for script in scripts:
            if isinstance(script, str):
                yield from shell_calls(script)
    else:
        yield from yaml_calls(child)


def markdown_calls(text: str) -> Iterator[list[str]]:
    """Read shell fences, and only prompt lines in console fences."""
    active: tuple[str, str | None] | None = None
    lines: list[str] = []
    for line in text.splitlines():
        match = FENCE.fullmatch(line)
        if match and active is None:
            active = (
                match[1],
                fence_language(match[2]),
            )
        elif active is not None and line.strip() == active[0]:
            yield from fence_calls(active[1], lines)
            active = None
            lines.clear()
        elif active is not None:
            if active[1] not in {"console", "sh-session"}:
                lines.append(line)
            elif line.startswith(("$ ", "# ")):
                lines.append(line[2:])
    if active is not None:
        yield from fence_calls(active[1], lines)


def fence_language(info: str) -> str | None:
    """The first info token names the fence language."""
    token = next(iter(info.split()), None)
    return token.strip("{}").removeprefix(".").lower() if token else None


def fence_calls(language: str | None, lines: list[str]) -> Iterator[list[str]]:
    """A supported shell fence, including an unclosed fence at end of file."""
    if language in SHELL_FENCES:
        yield from shell_calls("\n".join(lines))


def file_calls(path: Path, text: str) -> Iterator[list[str]]:
    """Select the supported parser by filename or shell shebang."""
    name = path.name.removesuffix(".j2")
    if name.endswith((".sh", ".bash", ".zsh")) or SHEBANG.match(text):
        yield from shell_calls(text)
    elif path.suffix == ".md":
        yield from markdown_calls(text)
    elif path.suffix == ".py":
        yield from python_calls(text)
    elif path.suffix in {".yml", ".yaml"}:
        try:
            for value in yaml.safe_load_all(text):
                yield from yaml_calls(value)
        except yaml.YAMLError:
            return
