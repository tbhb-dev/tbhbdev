# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Determine document contributors from edit receipts and sessions."""

from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.attribution import enrich
from tbhbdev.alpha.identity.claude_model import main_model
from tbhbdev.alpha.identity.frontmatter import ContributorKey, Entry, contributor_key
from tbhbdev.alpha.identity.payload import Json, rec_ts
from tbhbdev.alpha.identity.sessions import load_record
from tbhbdev.alpha.identity.trailers import utc
from tbhbdev.alpha.identity.values import (
    CLAUDE,
    SLUGS,
    Committer,
    Ident,
    Seen,
    make_ident,
)

if TYPE_CHECKING:
    from pathlib import Path


def contributor_of(ident: Ident | None, role: str, at: str) -> Entry | None:
    """A ``generated`` list item for an identity; None when its model is unknown."""
    if not ident or not ident.model:
        return None
    return {"role": role, **ident.attrs(), "at": at}


def keep_latest(group: dict[ContributorKey, Entry], entry: Entry | None) -> None:
    """Add the entry unless the group holds a later one for the same contributor."""
    if entry:
        key = contributor_key(entry)
        if key not in group or entry["at"] >= group[key]["at"]:
            group[key] = entry


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
    generated: dict[ContributorKey, Entry] = {}
    assisted: dict[ContributorKey, Entry] = {}
    for receipt in receipts:
        who = (
            enrich(Ident.from_receipt(receipt), committer)
            if committer
            else Ident.from_receipt(receipt)
        )
        at = utc(rec_ts(receipt))
        keep_latest(generated, contributor_of(who, "generated", at))
        if who.subagent:
            root = delegating_root(who, committer, base)
            keep_latest(assisted, contributor_of(root, "assisted", at))
    if not receipts and committer:
        keep_latest(generated, contributor_of(committer.ident, "generated", utc(now)))
    rest = [e for key, e in assisted.items() if key not in generated]
    return sorted([*generated.values(), *rest], key=lambda e: e["at"])
