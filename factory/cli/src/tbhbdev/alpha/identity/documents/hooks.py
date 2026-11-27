# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Gather contributors and stamp the documents staged for a commit."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
lazy from collections.abc import Mapping
lazy from pathlib import Path

from tbhbdev.alpha.identity import storage
from tbhbdev.alpha.identity.attribution import committer_identity
from tbhbdev.alpha.identity.claude.logs import main_model
from tbhbdev.alpha.identity.codex.rollout import refresh_codex
from tbhbdev.alpha.identity.commit.inputs import enrich
from tbhbdev.alpha.identity.documents.contributors import contributor_of, credit_edits
from tbhbdev.alpha.identity.documents.staging import staged_docs, stamp_document
from tbhbdev.alpha.identity.models import (
    CLAUDE,
    SLUGS,
    Committer,
    Ident,
    Seen,
    make_ident,
)
from tbhbdev.alpha.identity.receipts import all_edits
from tbhbdev.alpha.identity.records import Json, rec_ts
from tbhbdev.alpha.identity.sessions import load_record
from tbhbdev.alpha.identity.trailers import utc

if TYPE_CHECKING:
    from tbhbdev.alpha.identity.documents.frontmatter import Entry


def delegating_root(
    who: Ident, committer: Committer | None, base: Path
) -> Ident | None:
    """The root agent that delegated to subagent ``who``, if it can be identified."""
    if committer and committer.root_ident.root == who.root:
        return committer.root_ident
    if not who.harness.startswith(SLUGS[CLAUDE]):
        return None
    record = load_record(who.root, base)
    seen = Seen(
        record.get("version") or who.harness.partition(":")[2],
        main_model(record),
        record.get("effort"),
        who.surface,
    )
    return make_ident(CLAUDE, who.root, seen)


def stamp_entries(
    receipts: list[Json], committer: Committer | None, now: float, base: Path
) -> list[Entry]:
    """The contributors to stamp into one document, from its edit receipts.

    Each receipt's agent is ``generated`` at its latest edit of the file. A
    writer subagent's delegating root is ``assisted`` unless it wrote the file
    too. An agent with no receipt for the file gets nothing, except that with no
    receipts at all the committing agent is ``generated`` at ``now`` (a shell edit).
    """
    edits = []
    for receipt in receipts:
        who = Ident.from_receipt(receipt)
        if committer:
            who = enrich(who, committer)
        root = delegating_root(who, committer, base) if who.subagent else None
        edits.append((who, root, utc(rec_ts(receipt))))
    fallback = (
        contributor_of(committer.ident, "generated", utc(now))
        if not edits and committer
        else None
    )
    return credit_edits(edits, fallback)


def cmd_stamp_docs(env: Mapping[str, str]) -> int:
    """pre-commit: stamp ``generated`` into the frontmatter of staged documents.

    Receipts are read, not consumed (post-commit consumes them).
    """
    repo = storage.repo_info()
    if not repo:
        return 0
    committer = committer_identity(env)
    if committer:
        refresh_codex(committer, repo, env)
    receipts = all_edits(repo)
    now = time.time()
    # No cwd=None mutant matters below: the process already runs inside the repository.
    for doc in staged_docs(repo.top):  # pragma: no mutate
        mine = [r for r in receipts if r["path"] == doc[0]]
        entries = stamp_entries(mine, committer, now, repo.top)  # pragma: no mutate
        stamp_document(repo.top, doc, entries)
    return 0
