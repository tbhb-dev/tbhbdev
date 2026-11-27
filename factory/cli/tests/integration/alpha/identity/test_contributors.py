# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity contributors tests."""

from dataclasses import replace
from typing import TYPE_CHECKING

from integration.alpha.identity.identity_test_support import (
    AGENT,
    AT1,
    AT2,
    HAIKU,
    MAIN,
    OPUS,
    OTHER,
    ROOT,
    SESSION_HASH,
    STAMP_AT,
    Json,
    committed_as,
    entry,
    make_receipt,
    reply,
    write_transcript,
)

from tbhbdev.alpha.identity import contributors, frontmatter, sessions, storage, values
from tbhbdev.alpha.identity import trailers as identity_trailers

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tbhbdev.alpha.identity.frontmatter import Entry

WRITER = values.Ident(
    SESSION_HASH, "claude-code", AGENT, "anthropic:claude-haiku-5-5", "low", "cli"
)


def test_contributor_of() -> None:
    assert contributors.contributor_of(None, "generated", AT1) is None
    assert (
        contributors.contributor_of(values.Ident(ROOT, "codex"), "generated", AT1)
        is None
    )
    got = contributors.contributor_of(MAIN, "assisted", AT1)
    assert got == {
        "role": "assisted",
        "effort": "high",
        "harness": "claude-code",
        "model": "anthropic:claude-opus-5-5",
        "session": SESSION_HASH,
        "surface": "cli",
        "at": AT1,
    }
    assert got is not None
    assert frontmatter.valid_entry(got)


def test_keep_latest() -> None:
    group: dict[frontmatter.ContributorKey, Entry] = {}
    contributors.keep_latest(group, None)
    assert group == {}
    contributors.keep_latest(group, entry(at=AT2, effort="a"))
    contributors.keep_latest(group, entry(at=AT1, effort="b"))
    assert list(group.values()) == [entry(at=AT2, effort="a")]
    contributors.keep_latest(group, entry(at=AT2, effort="c"))
    assert list(group.values()) == [entry(at=AT2, effort="c")]
    contributors.keep_latest(group, entry(model="anthropic:other"))
    assert len(group) == 2


def test_delegating_root_prefers_the_committer_of_that_root(repo: Path) -> None:
    root = values.Ident(SESSION_HASH, "claude-code:9", None, "anthropic:root-model")
    committer = committed_as(MAIN, root)
    assert contributors.delegating_root(WRITER, committer, repo) is root
    foreign = values.Ident(OTHER, "claude-code", AGENT)
    rebuilt = contributors.delegating_root(foreign, committer, repo)
    assert rebuilt is not None
    assert rebuilt.root == OTHER


def test_delegating_root_rebuilds_a_claude_root_from_its_record(repo: Path) -> None:
    sessions.write_record(
        SESSION_HASH,
        {"version": "3.1", "model": "claude-opus-5-5", "effort": "max"},
        repo,
    )
    got = contributors.delegating_root(WRITER, None, repo)
    assert got == values.Ident(
        SESSION_HASH, "claude-code:3.1", None, "anthropic:claude-opus-5-5", "max", "cli"
    )
    versioned = replace(WRITER, root=OTHER, harness="claude-code:2.0")
    again = contributors.delegating_root(versioned, None, repo)
    assert again is not None
    assert again.harness == "claude-code:2.0"
    unversioned = contributors.delegating_root(replace(WRITER, root=OTHER), None, repo)
    assert unversioned is not None
    assert unversioned.harness == "claude-code"
    assert unversioned.model is None


def test_delegating_root_reads_the_record_in_the_given_directory(
    repo: Path, plain: Path
) -> None:
    # Only the repository's store has the record, not the user's.
    sessions = repo / ".git" / "tbhb-alpha" / "sessions"
    storage.write_json(sessions / f"{values.hx(SESSION_HASH)}.json", {"version": "3.1"})
    got = contributors.delegating_root(WRITER, None, repo)
    assert got is not None
    assert got.harness == "claude-code:3.1"
    assert plain.is_dir()


def test_delegating_root_knows_only_claude(repo: Path) -> None:
    for harness in ("codex", "antigravity:1.4"):
        child = values.Ident(SESSION_HASH, harness, AGENT)
        assert contributors.delegating_root(child, None, repo) is None
    assert contributors.delegating_root(
        values.Ident(SESSION_HASH, "claude-code"), None, repo
    )


def receipt_of(who: values.Ident, path: str, ts: float) -> Json:
    return make_receipt(path, who, None, ts)


def test_stamp_entries_one_agent_latest_edit_wins(repo: Path) -> None:
    receipts = [
        receipt_of(MAIN, "d.md", 1_800_000_000),
        receipt_of(MAIN, "d.md", 1_800_000_100),
    ]
    got = contributors.stamp_entries(receipts, None, 0, repo)
    assert got == [
        {
            "role": "generated",
            "effort": "high",
            "harness": "claude-code",
            "model": "anthropic:claude-opus-5-5",
            "session": SESSION_HASH,
            "surface": "cli",
            "at": identity_trailers.utc(1_800_000_100),
        }
    ]
    receipts.reverse()
    assert contributors.stamp_entries(receipts, None, 0, repo) == got


def test_stamp_entries_writer_subagent_makes_its_root_assisted(repo: Path) -> None:
    committer = committed_as(MAIN, replace(MAIN, harness="claude-code:2.1"))
    receipts = [receipt_of(WRITER, "d.md", 1_800_000_000)]
    got = contributors.stamp_entries(receipts, committer, 0, repo)
    assert [(e["role"], e.get("subagent"), e["harness"]) for e in got] == [
        ("generated", AGENT, "claude-code"),
        ("assisted", None, "claude-code:2.1"),
    ]
    assert all(e["at"] == identity_trailers.utc(1_800_000_000) for e in got)


def test_stamp_entries_root_that_wrote_too_is_not_assisted(repo: Path) -> None:
    receipts = [
        receipt_of(WRITER, "d.md", 1_800_000_000),
        receipt_of(MAIN, "d.md", 1_800_000_500),
    ]
    got = contributors.stamp_entries(receipts, committed_as(MAIN), 0, repo)
    assert [(e["role"], e.get("subagent")) for e in got] == [
        ("generated", AGENT),
        ("generated", None),
    ]
    assert [e["at"] for e in got] == [
        identity_trailers.utc(1_800_000_000),
        identity_trailers.utc(1_800_000_500),
    ]


def test_stamp_entries_sorts_by_time_and_only_subagents_get_roots(repo: Path) -> None:
    other = replace(MAIN, root=OTHER, model="anthropic:z")
    receipts = [
        receipt_of(other, "d.md", 1_800_000_900),
        receipt_of(MAIN, "d.md", 1_800_000_100),
    ]
    got = contributors.stamp_entries(receipts, None, 0, repo)
    assert [e["session"] for e in got] == [SESSION_HASH, OTHER]


def test_stamp_entries_enriches_receipts_from_the_committer(repo: Path) -> None:
    committer = committed_as(replace(MAIN, harness="claude-code:2.1.316"))
    got = contributors.stamp_entries(
        [receipt_of(MAIN, "d.md", 1_800_000_000)], committer, 0, repo
    )
    assert got[0]["harness"] == "claude-code:2.1.316"


def test_stamp_entries_skips_agents_without_a_model(repo: Path) -> None:
    unknown = replace(MAIN, model=None)
    assert (
        contributors.stamp_entries([receipt_of(unknown, "d.md", 1)], None, 0, repo)
        == []
    )
    blind = replace(WRITER, model=None)
    got = contributors.stamp_entries(
        [receipt_of(blind, "d.md", 1_800_000_000)], committed_as(MAIN), 0, repo
    )
    assert [(e["role"], e.get("subagent")) for e in got] == [("assisted", None)]


def test_stamp_entries_falls_back_to_the_committer_at_commit_time(repo: Path) -> None:
    committer = committed_as(replace(MAIN, harness="claude-code:2.1"))
    got = contributors.stamp_entries([], committer, 1_800_000_000, repo)
    assert [(e["role"], e["at"], e["harness"]) for e in got] == [
        ("generated", STAMP_AT, "claude-code:2.1")
    ]
    assert contributors.stamp_entries([], None, 1_800_000_000, repo) == []
    nameless = committed_as(replace(MAIN, model=None))
    assert contributors.stamp_entries([], nameless, 1_800_000_000, repo) == []
    # With receipts, the committer is not added for a file it did not edit.
    mine = [receipt_of(replace(MAIN, root=OTHER), "d.md", 1_800_000_000)]
    assert [
        e["session"] for e in contributors.stamp_entries(mine, committer, 0, repo)
    ] == [OTHER]


def test_stamp_entries_reads_the_root_record_from_the_given_base(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.write_record(
        SESSION_HASH, {"model": "claude-opus-5-5", "version": "3.1"}, repo
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    child = replace(WRITER, root=SESSION_HASH, model="anthropic:claude-haiku-5-5")
    got = contributors.stamp_entries(
        [receipt_of(child, "d.md", 1_800_000_000)], None, 0, repo
    )
    assert [(e["role"], e["model"], e["harness"]) for e in got] == [
        ("generated", "anthropic:claude-haiku-5-5", "claude-code"),
        ("assisted", "anthropic:claude-opus-5-5", "claude-code:3.1"),
    ]


def test_delegating_root_takes_the_main_model_at_commit_time(repo: Path) -> None:
    transcript = write_transcript(repo / "t.jsonl", reply(OPUS))
    sessions.write_record(
        SESSION_HASH,
        {"transcript": str(transcript), "model": HAIKU, "version": "3.1"},
        repo,
    )
    got = contributors.delegating_root(WRITER, None, repo)
    assert got is not None
    assert got.model == "anthropic:" + OPUS
