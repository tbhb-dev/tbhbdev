# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity attribution tests."""

import dataclasses
import json
from typing import TYPE_CHECKING

from integration.alpha.identity.identity_test_support import (
    AGENT,
    CHILD,
    OTHER,
    ROOT,
    SESSION_HASH,
    committer_of,
    make_ident,
    make_receipt,
)

from tbhbdev.alpha.identity import attribution, sessions, values

if TYPE_CHECKING:
    from pathlib import Path


def test_committer_identity_outside_harness() -> None:
    assert attribution.committer_identity({}) is None


def test_receipt_rank() -> None:
    who = make_ident()
    staged = {"a": "b1"}
    hit = make_receipt("a", who, blob="b1", ts=1)
    newer_miss = make_receipt("a", who, blob="zz", ts=9)
    no_blob = make_receipt("a", who, blob=None, ts=5)
    assert attribution.receipt_rank(hit, staged) == (0, -1)
    assert attribution.receipt_rank(newer_miss, staged) == (1, -9)
    assert attribution.receipt_rank(no_blob, staged) == (1, -5)
    assert attribution.receipt_rank(make_receipt("a", who, blob=None), {}) == (1, -1)
    assert sorted(
        [newer_miss, no_blob, hit], key=lambda r: attribution.receipt_rank(r, staged)
    ) == [hit, newer_miss, no_blob]


def test_decide_fallback_gives_the_committer_generated_by() -> None:
    who = make_ident()
    assert attribution.decide(committer_of(who), [], {"a"}, {}) == ([who], [])
    other = make_receipt("elsewhere", make_ident(CHILD))
    assert attribution.decide(committer_of(who), [other], {"a"}, {}) == ([who], [])


def test_decide_committer_who_wrote_is_not_assisted() -> None:
    who = make_ident()
    got = attribution.decide(committer_of(who), [make_receipt("a", who)], {"a"}, {})
    assert got == ([who], [])


def test_decide_committer_who_wrote_nothing_assists() -> None:
    writer = make_ident(CHILD)
    boss = make_ident()
    got = attribution.decide(committer_of(boss), [make_receipt("a", writer)], {"a"}, {})
    assert got == ([writer], [boss])


def test_decide_root_assists_when_a_child_commits() -> None:
    child_writer = make_ident(CHILD)
    other_child = make_ident(OTHER)
    root = make_ident()
    committer = committer_of(other_child, root)
    got = attribution.decide(committer, [make_receipt("a", child_writer)], {"a"}, {})
    assert got == ([child_writer], [other_child, root])
    # The root already wrote: it is not repeated. The child committer still assists.
    rooted = [make_receipt("a", root), make_receipt("b", child_writer)]
    got = attribution.decide(committer, rooted, {"a", "b"}, {})
    assert got[0] == [root, child_writer] or got[0] == [child_writer, root]
    assert got[1] == [other_child]
    # A root-level committer never adds the root twice.
    got = attribution.decide(
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
    gen, _ = attribution.decide(
        committer_of(make_ident()), receipts, {"a", "b"}, {"a": "mine"}
    )
    assert gen == [second, first]
    # Only receipts whose path is staged count.
    gen, _ = attribution.decide(committer_of(make_ident()), receipts, {"b"}, {})
    assert gen == [first]


def test_decide_different_model_same_agent_counts_as_written() -> None:
    a = make_ident(model="anthropic:one")
    b = make_ident(model="anthropic:two")
    gen, assisted = attribution.decide(
        committer_of(b), [make_receipt("a", a)], {"a"}, {}
    )
    assert gen == [a]
    assert assisted == []


def test_enrich_fills_harness_version_and_surface_from_the_committer() -> None:
    cm = committer_of(
        values.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", None, "cli")
    )
    bare = values.Ident(ROOT, "claude-code")
    # The same agent's missing model comes from the committer's root identity.
    assert attribution.enrich(bare, cm) == values.Ident(
        ROOT, "claude-code:2.1", None, "anthropic:m", None, "cli"
    )
    versioned = values.Ident(ROOT, "claude-code:1.0", None, None, None, "ide")
    assert attribution.enrich(versioned, cm) == dataclasses.replace(
        versioned, model="anthropic:m"
    )
    # Committer lacking either value fills nothing.
    plain_cm = committer_of(values.Ident(ROOT, "claude-code"))
    assert attribution.enrich(bare, plain_cm) == bare
    # A different harness never lends its values.
    codex_cm = values.Committer(
        values.CODEX,
        values.Ident(ROOT, "codex:9", None, None, None, "exec"),
        values.Ident(ROOT, "codex"),
    )
    assert attribution.enrich(bare, codex_cm) == bare


def test_enrich_child_model_and_effort_from_its_record(repo: Path) -> None:
    sessions.write_record(
        AGENT,
        {"root": SESSION_HASH, "model": "claude-haiku-5-5", "effort": "medium"},
        repo,
    )
    cm = committer_of(values.Ident(SESSION_HASH, "claude-code:2.1"))
    child = values.Ident(SESSION_HASH, "claude-code", AGENT)
    got = attribution.enrich(child, cm)
    assert (got.model, got.effort, got.harness) == (
        "anthropic:claude-haiku-5-5",
        "medium",
        "claude-code:2.1",
    )
    # Existing values win over the record.
    full = values.Ident(SESSION_HASH, "claude-code", AGENT, "anthropic:x", "low")
    assert attribution.enrich(full, cm) == replace_harness(full, "claude-code:2.1")
    half = values.Ident(SESSION_HASH, "claude-code", AGENT, "anthropic:x")
    got = attribution.enrich(half, cm)
    assert (got.model, got.effort) == ("anthropic:x", "medium")
    half = values.Ident(SESSION_HASH, "claude-code", AGENT, None, "low")
    got = attribution.enrich(half, cm)
    assert (got.model, got.effort) == ("anthropic:claude-haiku-5-5", "low")


def replace_harness(ident: values.Ident, harness: str) -> values.Ident:
    return values.Ident(
        ident.root, harness, ident.subagent, ident.model, ident.effort, ident.surface
    )


def test_enrich_fills_a_same_agent_main_identity_from_the_committer_root() -> None:
    root = values.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", "high", "cli")
    cm = committer_of(values.Ident(ROOT, "claude-code:2.1", CHILD, "anthropic:c"), root)
    bare = values.Ident(ROOT, "claude-code:2.1", None, None, None, "cli")
    assert attribution.enrich(bare, cm) == root
    # Each missing value is filled alone; a present one wins.
    only_effort = values.Ident(
        ROOT, "claude-code:2.1", None, "anthropic:x", None, "cli"
    )
    got = attribution.enrich(only_effort, cm)
    assert (got.model, got.effort) == ("anthropic:x", "high")
    only_model = values.Ident(ROOT, "claude-code:2.1", None, None, "low", "cli")
    got = attribution.enrich(only_model, cm)
    assert (got.model, got.effort) == ("anthropic:m", "low")
    # A root with nothing to lend fills nothing.
    empty = committer_of(cm.ident, values.Ident(ROOT, "claude-code:2.1"))
    assert attribution.enrich(bare, empty) == bare


def test_enrich_never_lends_the_root_model_to_another_agent() -> None:
    root = values.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", "high", "cli")
    cm = committer_of(root)
    other_root = values.Ident(OTHER, "claude-code:2.1", None, None, None, "cli")
    assert attribution.enrich(other_root, cm) == other_root
    # A subagent of the same session is a different agent.
    child = values.Ident(ROOT, "claude-code:2.1", CHILD, None, None, "cli")
    assert attribution.enrich(child, cm) == child
    # Only a Claude committer lends its root identity.
    codex_cm = values.Committer(
        values.CODEX, values.Ident(ROOT, "claude-code:2.1"), root
    )
    bare = values.Ident(ROOT, "claude-code:2.1", None, None, None, "cli")
    assert attribution.enrich(bare, codex_cm) == bare


def test_enrich_child_ignores_records_it_cannot_trust(repo: Path) -> None:
    sessions.write_record(
        AGENT, {"root": OTHER, "model": "claude-haiku-5-5", "effort": "medium"}, repo
    )
    cm = committer_of(values.Ident(SESSION_HASH, "claude-code"))
    child = values.Ident(SESSION_HASH, "claude-code", AGENT)
    assert attribution.enrich(child, cm) == child
    sessions.write_record(
        AGENT, {"root": SESSION_HASH, "model": "claude-haiku-5-5"}, repo
    )
    codex_cm = values.Committer(
        values.CODEX,
        values.Ident(SESSION_HASH, "codex"),
        values.Ident(SESSION_HASH, "codex"),
    )
    assert attribution.enrich(child, codex_cm) == child
    # A child of the main agent (no subagent) is never looked up.
    assert attribution.enrich(
        values.Ident(SESSION_HASH, "claude-code"), cm
    ) == values.Ident(SESSION_HASH, "claude-code")


def test_enrich_child_reads_the_transcript(repo: Path) -> None:
    transcript = repo / "t.jsonl"
    transcript.write_text(
        json.dumps({"message": {"model": "claude-haiku-5-5"}}) + "\n", encoding="utf-8"
    )
    sessions.write_record(
        AGENT, {"root": SESSION_HASH, "transcript": str(transcript)}, repo
    )
    cm = committer_of(values.Ident(SESSION_HASH, "claude-code"))
    got = attribution.enrich(values.Ident(SESSION_HASH, "claude-code", AGENT), cm)
    assert got.model == "anthropic:claude-haiku-5-5"
    assert got.effort is None


def test_enrich_matches_harness_ignoring_versions() -> None:
    cm = committer_of(
        values.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", None, "cli")
    )
    other = values.Ident(ROOT, "claude-code:1.0")
    assert attribution.enrich(other, cm) == values.Ident(
        ROOT, "claude-code:1.0", None, "anthropic:m", None, "cli"
    )


def test_run_model_updates_root_but_keeps_a_child_identity() -> None:
    root = make_ident(model=None)
    child = make_ident(CHILD, "anthropic:other", ROOT)
    committer = values.Committer(values.CLAUDE, child, root)
    updated = attribution.with_run_model(committer, "claude-opus-5-5")
    assert updated.ident == child
    assert updated.root_ident == dataclasses.replace(
        root, model="anthropic:claude-opus-5-5"
    )
    assert attribution.with_run_model(updated, "claude-sonnet-5-5") == updated
    assert attribution.with_run_model(committer, None) == committer
    assert (
        attribution.with_run_model(
            values.Committer(values.CLAUDE, root, root), "claude-opus-5-5"
        ).ident
        == updated.root_ident
    )


def test_enrich_compares_only_the_harness_slug() -> None:
    cm = committer_of(values.Ident(ROOT, "claude-code:2.1:x", None, "anthropic:m"))
    got = attribution.enrich(values.Ident(ROOT, "claude-code"), cm)
    assert got.harness == "claude-code:2.1:x"
