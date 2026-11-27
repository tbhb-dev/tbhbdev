# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Interpret shell command text for agent attribution and commit stamping."""

import hashlib
import re
import shlex
from itertools import dropwhile, takewhile
from pathlib import PurePath

# A control character, or the octal escape macOS ps writes for one.
CONTROL = re.compile(r"\\(?:0[0-3][0-7]|177)|[\x00-\x1f\x7f]")
AMEND_FLAGS = {"--am", "--ame", "--amen", "--amend"}  # git takes unique prefixes

# Git commands that rewrite work-tree files from a commit or the stash.
MOVING_GIT = frozenset(
    {
        "am",
        "checkout",
        "cherry-pick",
        "merge",
        "pull",
        "rebase",
        "reset",
        "restore",
        "revert",
        "stash",
        "switch",
    }
)
# Commands that write no work-tree content, so they cannot hide an edit.
QUIET_GIT = frozenset({"branch", "diff", "fetch", "log", "rev-parse", "show", "status"})
QUIET_TOOLS = frozenset(
    {"cat", "cd", "echo", "grep", "head", "ls", "pwd", "rg", "tail", "true", "wc"}
)
SEGMENT_BREAKS = re.compile(r"&&|\|\||[;|&\n]")
# A command or process substitution can run any command.
SUBSTITUTION = re.compile(r"\$\(|`|[<>]\(")
# Leading ``VAR=value`` words only set the environment.
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
# ``git rebase --exec`` runs arbitrary commands between the replayed commits.
EXEC_FLAGS = frozenset({"-x", "--exec"})
# Redirects that write nothing: ``2>&1`` and ``>/dev/null``.
HARMLESS_REDIRECT = re.compile(r"\d*>&\d+|\d*>>?\s*/dev/null")


def flat(cmd: str) -> str:
    r"""A command with each control character, or ps's octal escape of one, as a space.

    ps shows a multi-line command differently by platform: macOS writes a
    newline as ``\012`` and Linux writes it as a space. Both reduce to this.
    """
    return CONTROL.sub(" ", cmd)


def cmd_hashes(cmd: str) -> list[str]:
    """SHA-256 of a command as given, stripped (the harness may trim it) and flat."""
    forms = (cmd, cmd.strip(), flat(cmd), flat(cmd).strip())
    return sorted({hashlib.sha256(c.encode()).hexdigest() for c in forms})


def command_candidates(cmdline: str) -> list[str]:
    """Strings that could equal ``tool_input.command`` in one ancestor's command line.

    Claude Code (2.1.316) runs ``<shell> -c 'source <snapshot> ... &&
    eval '<command>' < /dev/null && pwd -P >| <file>'``.
    ps flattens argv, but the ``-c`` argument is itself shell text, so shlex
    recovers the eval operand exactly (embedded single quotes survive the
    ``'"'"'`` escape).
    """
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        return []
    return [tokens[i + 1] for i, tok in enumerate(tokens[:-1]) if tok == "eval"]


def git_verb(words: list[str]) -> str:
    """The subcommand of ``git``, skipping its global options."""
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in {"-C", "-c"} else 1
    return words[i] if i < len(words) else ""


def segment_kind(segment: str) -> str:
    """``move``, ``quiet`` or ``other`` for one simple command."""
    # The replacement text never holds ">", so mutating it cannot change the result.
    if ">" in HARMLESS_REDIRECT.sub("", segment):  # pragma: no mutate
        return "other"
    try:
        words = shlex.split(segment)
    except ValueError:
        return "other"
    words = list(dropwhile(ASSIGNMENT.match, words))
    if not words:
        return "quiet"
    if words[0] != "git":
        return "quiet" if words[0] in QUIET_TOOLS else "other"
    verb = git_verb(words)
    if verb == "rebase" and EXEC_FLAGS.intersection(words):
        return "other"
    return "move" if verb in MOVING_GIT else "quiet" if verb in QUIET_GIT else "other"


def moves_work_tree(cmd: object) -> bool:
    """Whether a command only moves the work tree with git.

    True when it runs a work-tree-moving git command and every other part is
    read-only. Any other command could have written content, so a line that
    mixes the two keeps its receipts.
    """
    if not isinstance(cmd, str):
        return False
    if SUBSTITUTION.search(cmd):
        return False
    plain = HARMLESS_REDIRECT.sub("", cmd)
    kinds = {segment_kind(s) for s in SEGMENT_BREAKS.split(plain)}
    return "move" in kinds and "other" not in kinds


def amending(command_lines: list[str]) -> bool:
    """Whether the nearest ``git ... commit`` in the ancestry has ``--amend``.

    ps flattens argv, so a message word ``--amend`` (``-m "fix --amend"``)
    reads as the flag too; the cost is only keeping the parent's trailers.
    """
    for line in command_lines:
        tokens = line.split()
        if tokens and PurePath(tokens[0]).name == "git" and "commit" in tokens:
            args = tokens[tokens.index("commit") + 1 :]
            return bool(AMEND_FLAGS & set(takewhile(lambda t: t != "--", args)))
    return False
