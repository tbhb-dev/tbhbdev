# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity commit roles."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import (
    CHILD,
    OTHER,
    ROOT,
    make_ident,
    make_receipt,
)

from tbhbdev.alpha.identity import models
from tbhbdev.alpha.identity.commit import roles as commit_roles


def committer_of(
    who: models.Ident, root: models.Ident | None = None
) -> models.Committer:
    return models.Committer(models.CLAUDE, who, root or who)


def test_receipt_rank() -> None:
    who = make_ident()
    staged = {"a": "b1"}
    hit = make_receipt("a", who, blob="b1", ts=1)
    newer_miss = make_receipt("a", who, blob="zz", ts=9)
    no_blob = make_receipt("a", who, blob=None, ts=5)
    assert commit_roles.receipt_rank(hit, staged) == (0, -1)
    assert commit_roles.receipt_rank(newer_miss, staged) == (1, -9)
    assert commit_roles.receipt_rank(no_blob, staged) == (1, -5)
    assert commit_roles.receipt_rank(make_receipt("a", who, blob=None), {}) == (1, -1)
    assert sorted(
        [newer_miss, no_blob, hit], key=lambda r: commit_roles.receipt_rank(r, staged)
    ) == [hit, newer_miss, no_blob]


def test_decide_fallback_gives_the_committer_generated_by() -> None:
    who = make_ident()
    assert commit_roles.decide(committer_of(who), [], {"a"}, {}) == ([who], [])
    other = make_receipt("elsewhere", make_ident(CHILD))
    assert commit_roles.decide(committer_of(who), [other], {"a"}, {}) == ([who], [])


def test_decide_committer_who_wrote_is_not_assisted() -> None:
    who = make_ident()
    got = commit_roles.decide(committer_of(who), [make_receipt("a", who)], {"a"}, {})
    assert got == ([who], [])


def test_decide_committer_who_wrote_nothing_assists() -> None:
    writer = make_ident(CHILD)
    boss = make_ident()
    got = commit_roles.decide(
        committer_of(boss), [make_receipt("a", writer)], {"a"}, {}
    )
    assert got == ([writer], [boss])


def test_decide_root_assists_when_a_child_commits() -> None:
    child_writer = make_ident(CHILD)
    other_child = make_ident(OTHER)
    root = make_ident()
    committer = committer_of(other_child, root)
    got = commit_roles.decide(committer, [make_receipt("a", child_writer)], {"a"}, {})
    assert got == ([child_writer], [other_child, root])
    # The root already wrote: it is not repeated. The child committer still assists.
    rooted = [make_receipt("a", root), make_receipt("b", child_writer)]
    got = commit_roles.decide(committer, rooted, {"a", "b"}, {})
    assert got[0] == [root, child_writer] or got[0] == [child_writer, root]
    assert got[1] == [other_child]
    # A root-level committer never adds the root twice.
    got = commit_roles.decide(
        committer_of(root), [make_receipt("a", child_writer)], {"a"}, {}
    )
    assert got == ([child_writer], [root])


def test_decide_orders_by_last_writer_then_age_and_dedupes() -> None:
    first, second = make_ident(CHILD), make_ident(OTHER)
    receipts = [
        make_receipt("a", first, blob="old", ts=1),
        make_receipt("a", second, blob="mine", ts=2),
        make_receipt("b", first, blob="x", ts=3),
    ]
    gen, _ = commit_roles.decide(
        committer_of(make_ident()), receipts, {"a", "b"}, {"a": "mine"}
    )
    assert gen == [second, first]
    # Only receipts whose path is staged count.
    gen, _ = commit_roles.decide(committer_of(make_ident()), receipts, {"b"}, {})
    assert gen == [first]


def test_decide_different_model_same_agent_counts_as_written() -> None:
    a = make_ident(model="anthropic:one")
    b = make_ident(model="anthropic:two")
    gen, assisted = commit_roles.decide(
        committer_of(b), [make_receipt("a", a)], {"a"}, {}
    )
    assert gen == [a]
    assert assisted == []


def test_enrich_fills_harness_version_and_surface_from_the_committer() -> None:
    cm = committer_of(
        models.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", None, "cli")
    )
    bare = models.Ident(ROOT, "claude-code")
    # The same agent's missing model comes from the committer's root identity.
    assert commit_roles.enrich_ident(bare, cm) == models.Ident(
        ROOT, "claude-code:2.1", None, "anthropic:m", None, "cli"
    )
    versioned = models.Ident(ROOT, "claude-code:1.0", None, None, None, "ide")
    assert commit_roles.enrich_ident(versioned, cm) == dataclasses.replace(
        versioned, model="anthropic:m"
    )
    # Committer lacking either value fills nothing.
    plain_cm = committer_of(models.Ident(ROOT, "claude-code"))
    assert commit_roles.enrich_ident(bare, plain_cm) == bare
    # A different harness never lends its values.
    codex_cm = models.Committer(
        models.CODEX,
        models.Ident(ROOT, "codex:9", None, None, None, "exec"),
        models.Ident(ROOT, "codex"),
    )
    assert commit_roles.enrich_ident(bare, codex_cm) == bare


def test_enrich_fills_a_same_agent_main_identity_from_the_committer_root() -> None:
    root = models.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", "high", "cli")
    cm = committer_of(models.Ident(ROOT, "claude-code:2.1", CHILD, "anthropic:c"), root)
    bare = models.Ident(ROOT, "claude-code:2.1", None, None, None, "cli")
    assert commit_roles.enrich_ident(bare, cm) == root
    # Each missing value is filled alone; a present one wins.
    only_effort = models.Ident(
        ROOT, "claude-code:2.1", None, "anthropic:x", None, "cli"
    )
    got = commit_roles.enrich_ident(only_effort, cm)
    assert (got.model, got.effort) == ("anthropic:x", "high")
    only_model = models.Ident(ROOT, "claude-code:2.1", None, None, "low", "cli")
    got = commit_roles.enrich_ident(only_model, cm)
    assert (got.model, got.effort) == ("anthropic:m", "low")
    # A root with nothing to lend fills nothing.
    empty = committer_of(cm.ident, models.Ident(ROOT, "claude-code:2.1"))
    assert commit_roles.enrich_ident(bare, empty) == bare


def test_enrich_never_lends_the_root_model_to_another_agent() -> None:
    root = models.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", "high", "cli")
    cm = committer_of(root)
    other_root = models.Ident(OTHER, "claude-code:2.1", None, None, None, "cli")
    assert commit_roles.enrich_ident(other_root, cm) == other_root
    # A subagent of the same session is a different agent.
    child = models.Ident(ROOT, "claude-code:2.1", CHILD, None, None, "cli")
    assert commit_roles.enrich_ident(child, cm) == child
    # Only a Claude committer lends its root identity.
    codex_cm = models.Committer(
        models.CODEX, models.Ident(ROOT, "claude-code:2.1"), root
    )
    bare = models.Ident(ROOT, "claude-code:2.1", None, None, None, "cli")
    assert commit_roles.enrich_ident(bare, codex_cm) == bare


def test_committing_keeps_staged_paths_except_still_unstaged_edits() -> None:
    who = make_ident()
    edits = [
        (Path(name), make_receipt(path, who, blob))
        for name, path, blob in (
            ("staged", "a.txt", "b1"),
            ("unstaged", "a.txt", "b2"),
            ("no-blob", "a.txt", None),
            ("other-path", "c.txt", "b2"),
            ("clean-path", "d.txt", "b2"),
            ("not-staged", "e.txt", "b1"),
        )
    ]
    got = commit_roles.committing(
        edits, {"a.txt", "c.txt", "d.txt"}, {"a.txt": "b2", "c.txt": "x"}
    )
    assert [path.name for path, _ in got] == [
        "staged",
        "no-blob",
        "other-path",
        "clean-path",
    ]


def test_enrich_matches_harness_ignoring_versions() -> None:
    cm = committer_of(
        models.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", None, "cli")
    )
    other = models.Ident(ROOT, "claude-code:1.0")
    assert commit_roles.enrich_ident(other, cm) == models.Ident(
        ROOT, "claude-code:1.0", None, "anthropic:m", None, "cli"
    )


def test_run_model_updates_root_but_keeps_a_child_identity() -> None:
    root = make_ident(model=None)
    child = make_ident(CHILD, "anthropic:other", ROOT)
    committer = models.Committer(models.CLAUDE, child, root)
    updated = commit_roles.with_run_model(committer, "claude-opus-5-5")
    assert updated.ident == child
    assert updated.root_ident == dataclasses.replace(
        root, model="anthropic:claude-opus-5-5"
    )
    assert commit_roles.with_run_model(updated, "claude-sonnet-5-5") == updated
    assert commit_roles.with_run_model(committer, None) == committer
    assert (
        commit_roles.with_run_model(
            models.Committer(models.CLAUDE, root, root), "claude-opus-5-5"
        ).ident
        == updated.root_ident
    )


def test_enrich_compares_only_the_harness_slug() -> None:
    cm = committer_of(models.Ident(ROOT, "claude-code:2.1:x", None, "anthropic:m"))
    got = commit_roles.enrich_ident(models.Ident(ROOT, "claude-code"), cm)
    assert got.harness == "claude-code:2.1:x"


@pytest.mark.parametrize(("edited", "assisted"), [(True, True), (False, False)])
def test_pick_roles_never_make_the_picker_generated(
    edited: bool, assisted: bool
) -> None:
    who = committer_of(make_ident())
    assert commit_roles.pick_roles(who, edited=edited) == (
        [],
        [who.ident] if assisted else [],
    )
