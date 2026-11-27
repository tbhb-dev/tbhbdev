# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity hook decisions over shell command text."""

import hashlib
import shlex

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tbhbdev.alpha.identity.commands import (
    amending,
    cmd_hashes,
    command_candidates,
    flat,
    git_verb,
    moves_work_tree,
    segment_kind,
)


@settings(database=None)
@given(
    st.lists(st.from_regex(r"[a-z0-9.]{1,6}", fullmatch=True), min_size=1, max_size=6)
)
def test_command_candidates_recover_eval_operand(parts: list[str]) -> None:
    cmd = " && ".join(f"echo 'it'\"'\"'s {p}'" for p in parts)
    line = f"/bin/zsh -c source s && eval {shlex.quote(cmd)} < /dev/null && pwd"
    assert command_candidates(line) == [cmd]


@settings(database=None)
@given(st.text(max_size=40))
def test_cmd_hashes_cover_trimmed_and_flat_forms(cmd: str) -> None:
    got = cmd_hashes(cmd)
    forms = {cmd, cmd.strip(), flat(cmd), flat(cmd).strip()}
    assert got == sorted(hashlib.sha256(f.encode()).hexdigest() for f in forms)


def test_flat_turns_control_characters_and_their_escapes_into_spaces() -> None:
    assert flat("a\nb\tc\x00d\x1fe\x7ff") == "a b c d e f"
    assert flat(r"a\012b\011c\000d\037e\177f") == "a b c d e f"
    assert flat(r"a\040b\08c\400d\12e") == r"a\040b\08c\400d\12e"
    assert flat("a b~") == "a b~"


def test_command_candidates_from_real_shape() -> None:
    cmd = "cd x && echo 'it'\"'\"'s' && git commit -m \"a b\""
    quoted = cmd.replace("'", "'\"'\"'")
    line = (
        "/bin/zsh -c source /u/shell-snapshots/s.sh 2>/dev/null || true && "
        "setopt NO_EXTENDED_GLOB 2>/dev/null || true && "
        "{ \\builtin unalias -- 'which'; } >/dev/null 2>&1 || true && "
        f"eval '{quoted}' < /dev/null && pwd -P >| /tmp/claude-1a2b-cwd"
    )
    assert command_candidates(line) == [cmd]
    assert command_candidates("python3 x.py trailer m") == []
    assert command_candidates("zsh -c 'unterminated") == []
    assert command_candidates("eval") == []
    assert command_candidates("a eval b eval c") == ["b", "c"]


def test_moves_work_tree_ignores_a_command_that_is_not_text() -> None:
    assert not moves_work_tree(None)
    assert not moves_work_tree(["git", "checkout"])
    assert moves_work_tree("git checkout a")


def test_moves_work_tree_strips_harmless_redirects_before_splitting() -> None:
    # Without the strip, the ``&`` of ``2>&1`` splits the line mid-redirect.
    assert moves_work_tree("git checkout a 2>&1")
    assert moves_work_tree("git checkout a >/dev/null 2>&1")
    # The strip leaves nothing behind, so it cannot glue onto the verb.
    assert moves_work_tree("git checkout>/dev/null")
    assert not moves_work_tree("git checkout a > out.txt")


@pytest.mark.parametrize(
    ("segment", "kind"),
    [
        ("echo hi > out.txt", "other"),
        ("echo 'unbalanced", "other"),
        ("", "quiet"),
        ("FOO=1 BAR=2", "quiet"),
        ("ls -l", "quiet"),
        ("FOO=1 cat x", "quiet"),
        ("rm x", "other"),
        ("git checkout a", "move"),
        ("git rebase main", "move"),
        ("git rebase --exec make main", "other"),
        ("git rebase -x make main", "other"),
        ("git status", "quiet"),
        ("git -C . log", "quiet"),
        ("git commit -m x", "other"),
        ("git", "other"),
    ],
)
def test_segment_kind_names_each_kind(segment: str, kind: str) -> None:
    assert segment_kind(segment) == kind


@pytest.mark.parametrize(
    ("words", "verb"),
    [
        (["git", "--no-pager", "log"], "log"),
        (["git", "-c", "a=b", "status"], "status"),
        (["git", "-C", ".", "--no-pager", "diff"], "diff"),
        (["git", "-C", "."], ""),
        (["git", "-C"], ""),
        (["git", "--no-pager"], ""),
        (["git"], ""),
    ],
)
def test_git_verb_skips_global_options(words: list[str], verb: str) -> None:
    assert git_verb(words) == verb


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("git commit --amend -m x", True),
        ("/opt/bin/git -C d commit -q --am", True),
        ("git commit --ame --no-edit", True),
        ("git commit --amen", True),
        ("git commit -m x -- --amend", False),
        ("git commit -m x", False),
        ("git commit --a", False),
        ("zsh -c git commit --amend", False),
        ("git log --amend", False),
        ("", False),
    ],
)
def test_amending(line: str, expected: bool) -> None:
    assert amending([line]) is expected


def test_amending_takes_the_nearest_git_commit() -> None:
    assert amending(["python x", "git commit -m x", "git commit --amend"]) is False
    assert amending(["python x", "git commit --amend", "git commit"]) is True
    assert amending([]) is False
