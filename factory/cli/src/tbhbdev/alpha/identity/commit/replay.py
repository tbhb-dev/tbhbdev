# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read Git state for amends, rebases and cherry-picks."""

from __future__ import annotations

from pathlib import Path

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import commands as shell_commands
from tbhbdev.alpha.identity.claude import ancestry as process_ancestry
from tbhbdev.alpha.identity.commit.picking import cherry_pick_problem, pick_source
from tbhbdev.alpha.identity.commit.policy import ident_email
from tbhbdev.alpha.identity.trailers import Trailer, llm_trailers


def amended_trailers(top: Path) -> list[Trailer]:
    """HEAD's ``LLM`` trailers when this commit replaces HEAD (``--amend``)."""
    if not shell_commands.amending(process_ancestry.ancestry()):
        return []
    _, msg = git_repo.git("log", "-1", "--format=%B", "HEAD", cwd=top)
    return llm_trailers(msg)


def rebasing() -> bool:
    """Whether a rebase is in progress; its replays set ``CHERRY_PICK_HEAD`` too."""
    # ``--git-path`` prints each path relative to the cwd, which is where Path looks.
    _, out = git_repo.git(
        "rev-parse", "--git-path", "rebase-merge", "--git-path", "rebase-apply"
    )
    return any(Path(line).is_dir() for line in out.splitlines())


def pick_head(*, replaying: bool) -> str:
    """The commit ``CHERRY_PICK_HEAD`` names; empty without one or in a replay."""
    return (
        ""
        if replaying
        else git_repo.git("rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD")[1]
    )


def picked_commit(
    head: str | None, text: str, top: Path
) -> tuple[str, list[Trailer], str] | None:
    """(sha, ``LLM`` trailers, author email) of the commit being picked, if any."""
    sha = pick_source(head, text)
    if not sha:
        return None
    code, out = git_repo.git("log", "-1", "--format=%ae%n%B", sha, cwd=top)
    if code != 0:  # a ``-x`` line can name a commit this repository lacks
        return None
    email, _, body = out.partition("\n")
    return sha, llm_trailers(body), email


def pick_refusal(
    picked: tuple[str, list[Trailer], str], text: str, top: Path
) -> str | None:
    """The message refusing this pick, or None to let it through."""
    sha, src_trailers, src_author = picked
    _, ident = git_repo.git("var", "GIT_COMMITTER_IDENT", cwd=top)
    # A trailer the picker added by hand, as the refusal asks, counts too.
    return cherry_pick_problem(
        sha, src_trailers, text, own=src_author == ident_email(ident)
    )
