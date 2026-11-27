# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity attribution."""

from __future__ import annotations

lazy from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import (
    EARLIER,
    EARLIER_TS,
    HAIKU,
    OPUS,
    SESSION_HASH,
    SONNET,
)
from unit.alpha.identity.identity_fixtures import (
    CLAUDE_COMMIT_ENV,
    CLAUDE_HOOK_ENV,
    add_worktree,
    reply,
    session_file,
    switch_data,
    write_transcript,
)

from tbhbdev.alpha.identity import attribution, sessions, storage
from tbhbdev.alpha.identity.claude import hooks as claude_hooks


def test_committer_identity_outside_harness() -> None:
    assert attribution.committer_identity({}) is None


def test_a_newer_switch_in_another_worktree_wins(repo: Path, tmp_path: Path) -> None:
    """Switch to A in W1, to B in W2, commit from W1: the commit gets B."""
    tree = add_worktree(repo, tmp_path / "wt")
    transcript = write_transcript(tmp_path / "t.jsonl", reply(OPUS))
    for where, model in ((repo, HAIKU), (tree, SONNET)):
        data = {
            **switch_data(repo, model),
            "cwd": str(where),
            "transcript_path": str(transcript),
        }
        claude_hooks.handle_claude(data, CLAUDE_HOOK_ENV)
    assert (
        storage.read_record(session_file(repo / ".git" / "tbhb-alpha"))["switch_model"]
        == HAIKU
    )
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, [])
    assert got is not None
    assert got.ident.model == "anthropic:" + SONNET


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [(EARLIER, OPUS), ("not a time", SONNET)],
    ids=["tie-goes-to-the-transcript", "unreadable-time-lets-the-switch-win"],
)
def test_a_commit_compares_the_switch_with_the_transcript_time(
    repo: Path, timestamp: str, expected: str
) -> None:
    transcript = write_transcript(repo / "t.jsonl", reply(OPUS, timestamp))
    sessions.write_record(
        SESSION_HASH,
        {
            "transcript": str(transcript),
            "switch_model": SONNET,
            "switch_at": EARLIER_TS,
        },
        repo,
    )
    got = attribution.committer_identity(CLAUDE_COMMIT_ENV, [])
    assert got is not None
    assert got.ident.model == "anthropic:" + expected
