# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity documents contributors."""

from __future__ import annotations

from unit.alpha.identity.identity_examples import (
    AT1,
    AT2,
    CLAUDE_TRAILER_ATTRS,
    MAIN,
    ROOT,
    entry,
)

from tbhbdev.alpha.identity import models
from tbhbdev.alpha.identity.documents import contributors as documents_contributors
from tbhbdev.alpha.identity.documents import frontmatter as documents_frontmatter
lazy from tbhbdev.alpha.identity.documents.frontmatter import Entry


def test_contributor_of() -> None:
    assert documents_contributors.contributor_of(None, "generated", AT1) is None
    assert (
        documents_contributors.contributor_of(
            models.Ident(ROOT, "codex"), "generated", AT1
        )
        is None
    )
    got = documents_contributors.contributor_of(MAIN, "assisted", AT1)
    assert got == {
        "role": "assisted",
        **CLAUDE_TRAILER_ATTRS,
        "at": AT1,
    }
    assert got is not None
    assert documents_frontmatter.valid_entry(got)


def test_keep_latest() -> None:
    group: dict[documents_frontmatter.ContributorKey, Entry] = {}
    documents_contributors.keep_latest(group, None)
    assert group == {}
    documents_contributors.keep_latest(group, entry(at=AT2, effort="a"))
    documents_contributors.keep_latest(group, entry(at=AT1, effort="b"))
    assert list(group.values()) == [entry(at=AT2, effort="a")]
    documents_contributors.keep_latest(group, entry(at=AT2, effort="c"))
    assert list(group.values()) == [entry(at=AT2, effort="c")]
    documents_contributors.keep_latest(group, entry(model="anthropic:other"))
    assert len(group) == 2
