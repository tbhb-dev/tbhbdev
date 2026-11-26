# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-ssh-agent`: keep agent commands off the SSH agent.

This PreToolUse hook shares guard-gh-pr's payload contracts and uses a small
shell lexer. It checks simple commands and brace groups for `ssh-add`, with
an exception for the read-only `ssh-add -l`. It skips plain assignments and
option-less wrappers. It also denies tool `file_path` and `path` values that
resolve into the operator's ssh directory outside `sessions`, or to a Unix
socket outside the ssh directory. Paths resolve against the payload's cwd.

The hook denies invalid UTF-8 and unterminated quotes in commands it reads.
An allow prints nothing. Invalid JSON and crashes, including a closed output
pipe, fail open.

Known gaps include redirections, heredocs, `case`, parentheses, `${` expansions,
command substitutions, backticks, functions, wrappers with options, `sudo`,
`doas`, variable command names, runners such as `timeout`, `xargs` and `bash -c`,
and keywords such as `then` before `ssh-add`. Shell arguments naming the ssh
directory, case variants of that directory on case-insensitive volumes, and
Antigravity file tools and `workspacePaths` are also outside the check.
Comparisons and regexes in `[[` tests that use unsupported syntax stay outside
the check to keep the lexer small.
ANSI-C quoting keeps text together, with full escape decoding left as debt.
"""

import json
import os
import posixpath
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.guard_gh_pr import command_of, decision

if TYPE_CHECKING:
    from tbhbdev.alpha.guard_gh_pr import Json

REASON_ADD = "ssh-add is blocked: agent sessions never load keys into an agent."
REASON_DIR = (
    "The call is blocked: it names a path in the operator's ssh directory "
    "outside its sessions subdirectory."
)
REASON_SOCKET = "The call is blocked: it names a Unix socket."
REASON_QUOTE = "The call is blocked: the command has an unterminated quote."
REASON_ENCODING = "The call is blocked: the hook payload must be UTF-8."
SSH_DIR = ".ssh"
OPERATORS = ";&|"
# Constructs the lexer does not handle: the whole command is allowed.
UNPARSED = re.compile(r"[()`<>]|\$\{|\bcase\b")
GROUP_OPEN = re.compile(r"\{(?=[ \t\r\n]|$)")
TEST_OPEN = re.compile(r"\[\[(?=[ \t\r\n]|$)")
TEST_CLOSE = re.compile(r"\]\](?=[ \t\r\n;&|]|$)")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
WRAPPERS = {"command", "time", "env"}
SYSTEM_WRAPPERS = {"exec", "nohup"}
SYSTEM_DIRS = {
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
}
PATH_FIELDS = ("file_path", "path")


class UnsupportedSyntaxError(Exception):
    """An expansion the lexer cannot read inside double quotes."""


def quoted(text: str, index: int, mark: str, *, ansi: bool = False) -> tuple[str, int]:
    """The text up to the MARK closing the quote at INDEX and the index after it.

    Raises:
        ValueError: the quote is not closed.
        UnsupportedSyntaxError: the quote contains an unsupported expansion.
    """
    out: list[str] = []
    index += 1
    while index < len(text) and text[index] != mark:
        if text[index] == "\\":
            following = text[index + 1 : index + 2]
            if mark == '"' and following == "\n":
                index += 2
                continue
            if (mark == '"' and following in {'"', "\\", "$", "`"}) or (
                ansi and following in {"'", "\\"}
            ):
                index += 1
        elif mark == '"' and text.startswith(("$(", "${", "`"), index):
            raise UnsupportedSyntaxError
        out.append(text[index])
        index += 1
    if index >= len(text):
        raise ValueError(mark)
    return "".join(out), index + 1


class Lexer:
    """Reads shell text into segments, tracking the word being built."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0
        self.found: list[list[str]] = [[]]
        self.buf: list[str] = []
        self.started = False
        self.in_test = False

    def flush(self) -> None:
        """End the word being built."""
        if self.started:
            self.found[-1].append("".join(self.buf))
        self.buf.clear()
        self.started = False

    def quote(self, mark: str, *, ansi: bool = False) -> None:
        """Read the quoted part at the index."""
        part, self.index = quoted(self.text, self.index, mark, ansi=ansi)
        self.buf.append(part)
        self.started = True

    def escape(self) -> None:
        """Read a backslash and the character it escapes."""
        following = self.text[self.index + 1 : self.index + 2]
        self.index += 2
        if following != "\n":
            self.buf.append(following)
            self.started = True

    def operator(self) -> None:
        """Read the operator at the index, which starts the next segment."""
        self.flush()
        self.index += 1
        self.found.append([])

    def compound(self) -> bool:
        """Consume the opening of a brace group or either delimiter of a test."""
        if self.started:
            return False
        if self.in_test and TEST_CLOSE.match(self.text, self.index):
            self.in_test = False
            self.index += 2
        elif self.found[-1]:
            return False
        elif GROUP_OPEN.match(self.text, self.index):
            self.index += 1
        elif TEST_OPEN.match(self.text, self.index):
            self.found[-1].append("[[")
            self.in_test = True
            self.index += 2
        else:
            return False
        return True

    def step(self) -> None:
        """Read one character or construct at the index."""
        char = self.text[self.index]
        if self.text.startswith("$'", self.index):
            self.index += 1
            self.quote("'", ansi=True)
        elif char in "'\"":
            self.quote(char)
        elif char == "\\":
            self.escape()
        elif char == "#" and not self.started:
            self.index = self.text.find("\n", self.index) % (len(self.text) + 1)
        elif char in "{[]" and self.compound():
            return
        elif char in " \t\r" or (self.in_test and char in "\n" + OPERATORS):
            self.flush()
            self.index += 1
        elif char == "\n" or char in OPERATORS:
            self.operator()
        else:
            self.buf.append(char)
            self.started = True
            self.index += 1


def segments(text: str) -> list[list[str]]:
    """The simple commands of TEXT, split at the shell operators outside quotes.

    Raises:
        ValueError: TEXT has an unbalanced quote.
    """
    unparsed = UNPARSED.search(text) is not None
    lexer = Lexer(text)
    try:
        while lexer.index < len(text):
            if unparsed and UNPARSED.match(text, lexer.index):
                return []
            lexer.step()
    except UnsupportedSyntaxError:
        return []
    # Preserve the existing fallback after checking quotes in literal text.
    if unparsed:
        return []
    lexer.flush()
    return [words for words in lexer.found if words]


def blocks_ssh_add(words: list[str]) -> bool:
    """Whether the command after assignments and option-less wrappers is blocked.

    `command`, `time` and `env` match by basename. `exec` and `nohup` match
    as bare names or paths in a system directory.
    """
    wrapper = ""
    for index, word in enumerate(words):
        if wrapper and word.startswith("-"):
            return False
        directory, name = posixpath.split(word)
        if not (
            ASSIGNMENT.match(word)
            or name in WRAPPERS
            or (
                name in SYSTEM_WRAPPERS
                and (not directory or posixpath.normpath(directory) in SYSTEM_DIRS)
            )
        ):
            return name == "ssh-add" and words[index + 1 :] != ["-l"]
        wrapper = name
    return False


def expand(text: str, home: str) -> str:
    """TEXT with a leading `~` or `$HOME` expanded."""
    for prefix in ("~", "$HOME", "${HOME}"):
        if text == prefix or text.startswith(prefix + "/"):
            return home + text[len(prefix) :]
    return text


def in_sessions(path: str, ssh: str, sessions: str) -> bool:
    """Whether PATH, already below SSH, is in the sessions directory.

    The first component is compared by file identity too, so `SESSIONS` names
    the same directory on a case-insensitive volume.
    """
    if path == sessions or path.startswith(sessions + "/"):
        return True
    first = path[len(ssh) + 1 :].split("/", 1)[0]
    try:
        return Path(f"{ssh}/{first}").samefile(sessions)
    except OSError:
        return False


def file_reason(text: str, cwd: str) -> str | None:
    """The deny reason for a file named by a tool, or None."""
    if not text:
        return None
    home = str(Path.home())
    text = expand(text, home)
    if not Path(text).is_absolute():
        full = str(Path(cwd) / text)
        # A bare name is a path only if it exists, so `git log main` is allowed.
        if (
            "/" not in text
            and not text.startswith(".")
            and not Path(full).is_symlink()
            and not Path(full).exists()
        ):
            return None
        text = full
    path = os.path.realpath(text)
    ssh = os.path.realpath(f"{home}/{SSH_DIR}")
    if path == ssh or path.startswith(ssh + "/"):
        sessions = os.path.realpath(f"{ssh}/sessions")
        return None if in_sessions(path, ssh, sessions) else REASON_DIR
    return REASON_SOCKET if Path(path).is_socket() else None


def verdict(text: str) -> str | None:
    """The deny reason for a shell command, or None to allow it."""
    try:
        parsed = segments(text)
    except ValueError:
        return REASON_QUOTE
    return REASON_ADD if any(blocks_ssh_add(words) for words in parsed) else None


def path_reason(data: Json, cwd: str | None = None) -> str | None:
    """The deny reason for a path field of the tool input, or None."""
    inputs = data.get("tool_input")
    if not isinstance(inputs, dict):
        return None
    cwd = cwd or str(Path.cwd())
    for field in PATH_FIELDS:
        value = inputs.get(field)
        if isinstance(value, str) and (reason := file_reason(value, cwd)):
            return reason
    return None


def read_payload() -> tuple[Json, str | None]:
    """Read hook JSON with a distinct reason for invalid UTF-8."""
    buffer = getattr(sys.stdin, "buffer", None)
    # Preserve the JSON envelope so a bad byte can still get a harness-specific denial.
    try:
        text = (
            sys.stdin.read()
            if buffer is None
            else buffer.read().decode("utf-8", errors="surrogateescape")
        )
    except UnicodeError:
        return {}, REASON_ENCODING
    reason = None
    try:
        text.encode("utf-8")
    except UnicodeError:
        reason = REASON_ENCODING
    try:
        data = json.loads(text)
    except ValueError:
        return {}, reason
    return (data if isinstance(data, dict) else {}), reason


def denial() -> str | None:
    """The deny decision as JSON for the payload on stdin, or None to allow."""
    data, encoding_reason = read_payload()
    text, antigravity = command_of(data)
    cwd = data.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None
    reason = encoding_reason or verdict(text) or path_reason(data, cwd)
    return json.dumps(decision(reason, antigravity=antigravity)) if reason else None


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        blocked = denial()
        if blocked:
            print(blocked)
            sys.stdout.flush()
    except Exception:  # noqa: BLE001  # fail open on any crash, even a closed pipe
        return 0
    return 0
