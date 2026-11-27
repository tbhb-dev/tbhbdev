# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Build generated and assisted frontmatter entries from identities."""

from __future__ import annotations

from tbhbdev.alpha.identity.documents.frontmatter import (
    ContributorKey,
    Entry,
    contributor_key,
)
lazy from tbhbdev.alpha.identity.models import Ident


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


def credit_edits(
    edits: list[tuple[Ident, Ident | None, str]],
    fallback: Entry | None,
) -> list[Entry]:
    """Credit document writers and their delegating agents in edit order."""
    generated: dict[ContributorKey, Entry] = {}
    assisted: dict[ContributorKey, Entry] = {}
    for writer, root, at in edits:
        keep_latest(generated, contributor_of(writer, "generated", at))
        keep_latest(assisted, contributor_of(root, "assisted", at))
    if not edits:
        keep_latest(generated, fallback)
    rest = [entry for key, entry in assisted.items() if key not in generated]
    return sorted([*generated.values(), *rest], key=lambda entry: entry["at"])
