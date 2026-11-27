# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Recover attribution when Git amends, rebases, or cherry-picks commits."""

import re
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity import commands as identity_commands
from tbhbdev.alpha.identity.ancestry import ancestry
from tbhbdev.alpha.identity.trailers import KEY_GEN
from tbhbdev.alpha.identity.validation import Trailer, ident_email, llm_trailers
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from tbhbdev.alpha.identity.values import Committer, Ident

PICKED_FROM = re.compile(
    r"^\(cherry picked from commit ([0-9a-f]{40})\)$", re.MULTILINE
)

MODEL_ATTR = re.compile(r"(?<![^\s;])model=[^\s;]")


def amended_trailers(top: Path) -> list[Trailer]:
    """HEAD's ``LLM`` trailers when this commit replaces HEAD (``--amend``)."""
    if not identity_commands.amending(ancestry()):
        return []
    _, msg = git("log", "-1", "--format=%B", "HEAD", cwd=top)
    return llm_trailers(msg)


def rebasing() -> bool:
    """Whether a rebase is in progress; its replays set ``CHERRY_PICK_HEAD`` too."""
    # ``--git-path`` prints each path relative to the cwd, which is where Path looks.
    _, out = git(
        "rev-parse", "--git-path", "rebase-merge", "--git-path", "rebase-apply"
    )
    return any(Path(line).is_dir() for line in out.splitlines())


def pick_head(*, replaying: bool) -> str:
    """The commit ``CHERRY_PICK_HEAD`` names; empty without one or in a replay."""
    return (
        "" if replaying else git("rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD")[1]
    )


def pick_source(head: str | None, text: str) -> str | None:
    """The picked commit: ``CHERRY_PICK_HEAD``, else the last ``-x`` line."""
    found = PICKED_FROM.findall(text)
    return head or (found[-1] if found else None)


def pick_refused(src_trailers: list[Trailer], *, own: bool) -> bool:
    """Whether to refuse a pick: our own agent's commit with no model named."""
    return own and not any(
        key == KEY_GEN and MODEL_ATTR.search(value) for key, value in src_trailers
    )


def usable_trailers(src_trailers: list[Trailer]) -> list[Trailer]:
    """The trailers worth keeping; commit-msg rejects a ``Generated-by`` sans model."""
    return [
        (key, value)
        for key, value in src_trailers
        if key != KEY_GEN or MODEL_ATTR.search(value)
    ]


def picked_commit(
    head: str | None, text: str, top: Path
) -> tuple[str, list[Trailer], str] | None:
    """(sha, ``LLM`` trailers, author email) of the commit being picked, if any."""
    sha = pick_source(head, text)
    if not sha:
        return None
    code, out = git("log", "-1", "--format=%ae%n%B", sha, cwd=top)
    if code != 0:  # a ``-x`` line can name a commit this repository lacks
        return None
    email, _, body = out.partition("\n")
    return sha, llm_trailers(body), email


def pick_roles(
    committer: Committer, *, edited: bool
) -> tuple[list[Ident], list[Ident]]:
    """A picker wrote none of the change: it assists when it edited, else no one."""
    return [], [committer.ident] if edited else []


def pick_refusal(
    picked: tuple[str, list[Trailer], str], text: str, top: Path
) -> str | None:
    """The message refusing this pick, or None to let it through."""
    sha, src_trailers, src_author = picked
    _, ident = git("var", "GIT_COMMITTER_IDENT", cwd=top)
    # A trailer the picker added by hand, as the refusal asks, counts too.
    present = [*llm_trailers(text), *src_trailers]
    if not pick_refused(present, own=src_author == ident_email(ident)):
        return None
    short = sha[:7]
    return (
        f"cherry-pick of {short} has no Generated-by trailer (with model=); "
        f"add the source's trailer by hand (git log -1 --format=%B {short}) "
        "and commit again"
    )
