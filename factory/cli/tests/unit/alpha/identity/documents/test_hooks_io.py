# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity documents hooks."""

from __future__ import annotations

import dataclasses
import time
from typing import cast
lazy from pathlib import Path

import jsonschema
import yaml
from unit.alpha.identity.identity_examples import (
    CLAUDE_TRAILER_ATTRS,
    HAIKU,
    MAIN,
    OPUS,
    OTHER,
    SESSION_HASH,
    Json,
    document,
    make_receipt,
    patch_call,
)
from unit.alpha.identity.identity_fixtures import (
    AGENT,
    CLAUDE_COMMIT_ENV,
    DATA,
    ROOT_THREAD,
    committed_as,
    edit_by,
    loaded,
    meta,
    receipts_of,
    reply,
    stage_doc,
    staged_text,
    turn,
    write_rollout,
    write_transcript,
)
lazy import pytest

from tbhbdev.alpha.identity import models, records, sessions, storage, trailers
from tbhbdev.alpha.identity.documents import hooks as documents_hooks
lazy from tbhbdev.alpha.identity.documents.frontmatter import Entry

SCHEMA = loaded(DATA / "frontmatter.schema.json")


VALIDATOR = jsonschema.Draft202012Validator(
    SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)


def front_of(text: str) -> Json:
    """The parsed frontmatter, checked against the repo's schema."""
    data = yaml.safe_load(text.split("---\n")[1])
    VALIDATOR.validate(data)
    return cast("Json", data)


WRITER = models.Ident(
    SESSION_HASH, "claude-code", AGENT, "anthropic:claude-haiku-5-5", "low", "cli"
)


STAMP_AT = trailers.utc(1_800_000_000)


def test_delegating_root_prefers_the_committer_of_that_root(repo: Path) -> None:
    root = models.Ident(SESSION_HASH, "claude-code:9", None, "anthropic:root-model")
    committer = committed_as(MAIN, root)
    assert documents_hooks.delegating_root(WRITER, committer, repo) is root
    foreign = models.Ident(OTHER, "claude-code", AGENT)
    rebuilt = documents_hooks.delegating_root(foreign, committer, repo)
    assert rebuilt is not None
    assert rebuilt.root == OTHER


def test_delegating_root_rebuilds_a_claude_root_from_its_record(repo: Path) -> None:
    sessions.write_record(
        SESSION_HASH,
        {"version": "3.1", "model": "claude-opus-5-5", "effort": "max"},
        repo,
    )
    got = documents_hooks.delegating_root(WRITER, None, repo)
    assert got == models.Ident(
        SESSION_HASH, "claude-code:3.1", None, "anthropic:claude-opus-5-5", "max", "cli"
    )
    versioned = dataclasses.replace(WRITER, root=OTHER, harness="claude-code:2.0")
    again = documents_hooks.delegating_root(versioned, None, repo)
    assert again is not None
    assert again.harness == "claude-code:2.0"
    unversioned = documents_hooks.delegating_root(
        dataclasses.replace(WRITER, root=OTHER), None, repo
    )
    assert unversioned is not None
    assert unversioned.harness == "claude-code"
    assert unversioned.model is None


def test_delegating_root_reads_the_record_in_the_given_directory(
    repo: Path, plain_checkout: Path
) -> None:
    # Only the repository's store has the record, not the user's.
    sessions = repo / ".git" / "tbhb-alpha" / "sessions"
    storage.write_json(
        sessions / f"{records.hx(SESSION_HASH)}.json", {"version": "3.1"}
    )
    got = documents_hooks.delegating_root(WRITER, None, repo)
    assert got is not None
    assert got.harness == "claude-code:3.1"
    assert plain_checkout.is_dir()


def test_delegating_root_knows_only_claude(repo: Path) -> None:
    for harness in ("codex", "antigravity:1.4"):
        child = models.Ident(SESSION_HASH, harness, AGENT)
        assert documents_hooks.delegating_root(child, None, repo) is None
    assert documents_hooks.delegating_root(
        models.Ident(SESSION_HASH, "claude-code"), None, repo
    )


def receipt_of(who: models.Ident, path: str, ts: float) -> Json:
    return make_receipt(path, who, None, ts)


def test_stamp_entries_one_agent_latest_edit_wins(repo: Path) -> None:
    receipts = [
        receipt_of(MAIN, "d.md", 1_800_000_000),
        receipt_of(MAIN, "d.md", 1_800_000_100),
    ]
    got = documents_hooks.stamp_entries(receipts, None, 0, repo)
    assert got == [
        {
            "role": "generated",
            **CLAUDE_TRAILER_ATTRS,
            "at": trailers.utc(1_800_000_100),
        }
    ]
    receipts.reverse()
    assert documents_hooks.stamp_entries(receipts, None, 0, repo) == got


def test_stamp_entries_writer_subagent_makes_its_root_assisted(repo: Path) -> None:
    committer = committed_as(MAIN, dataclasses.replace(MAIN, harness="claude-code:2.1"))
    receipts = [receipt_of(WRITER, "d.md", 1_800_000_000)]
    got = documents_hooks.stamp_entries(receipts, committer, 0, repo)
    assert [(e["role"], e.get("subagent"), e["harness"]) for e in got] == [
        ("generated", AGENT, "claude-code"),
        ("assisted", None, "claude-code:2.1"),
    ]
    assert all(e["at"] == trailers.utc(1_800_000_000) for e in got)


def test_stamp_entries_root_that_wrote_too_is_not_assisted(repo: Path) -> None:
    receipts = [
        receipt_of(WRITER, "d.md", 1_800_000_000),
        receipt_of(MAIN, "d.md", 1_800_000_500),
    ]
    got = documents_hooks.stamp_entries(receipts, committed_as(MAIN), 0, repo)
    assert [(e["role"], e.get("subagent")) for e in got] == [
        ("generated", AGENT),
        ("generated", None),
    ]
    assert [e["at"] for e in got] == [
        trailers.utc(1_800_000_000),
        trailers.utc(1_800_000_500),
    ]


def test_stamp_entries_sorts_by_time_and_only_subagents_get_roots(repo: Path) -> None:
    other = dataclasses.replace(MAIN, root=OTHER, model="anthropic:z")
    receipts = [
        receipt_of(other, "d.md", 1_800_000_900),
        receipt_of(MAIN, "d.md", 1_800_000_100),
    ]
    got = documents_hooks.stamp_entries(receipts, None, 0, repo)
    assert [e["session"] for e in got] == [SESSION_HASH, OTHER]


def test_stamp_entries_enriches_receipts_from_the_committer(repo: Path) -> None:
    committer = committed_as(dataclasses.replace(MAIN, harness="claude-code:2.1.316"))
    got = documents_hooks.stamp_entries(
        [receipt_of(MAIN, "d.md", 1_800_000_000)], committer, 0, repo
    )
    assert got[0]["harness"] == "claude-code:2.1.316"


def test_stamp_entries_skips_agents_without_a_model(repo: Path) -> None:
    unknown = dataclasses.replace(MAIN, model=None)
    assert (
        documents_hooks.stamp_entries([receipt_of(unknown, "d.md", 1)], None, 0, repo)
        == []
    )
    blind = dataclasses.replace(WRITER, model=None)
    got = documents_hooks.stamp_entries(
        [receipt_of(blind, "d.md", 1_800_000_000)], committed_as(MAIN), 0, repo
    )
    assert [(e["role"], e.get("subagent")) for e in got] == [("assisted", None)]


def test_stamp_entries_falls_back_to_the_committer_at_commit_time(repo: Path) -> None:
    committer = committed_as(dataclasses.replace(MAIN, harness="claude-code:2.1"))
    got = documents_hooks.stamp_entries([], committer, 1_800_000_000, repo)
    assert [(e["role"], e["at"], e["harness"]) for e in got] == [
        ("generated", STAMP_AT, "claude-code:2.1")
    ]
    assert documents_hooks.stamp_entries([], None, 1_800_000_000, repo) == []
    nameless = committed_as(dataclasses.replace(MAIN, model=None))
    assert documents_hooks.stamp_entries([], nameless, 1_800_000_000, repo) == []
    # With receipts, the committer is not added for a file it did not edit.
    mine = [receipt_of(dataclasses.replace(MAIN, root=OTHER), "d.md", 1_800_000_000)]
    assert [
        e["session"] for e in documents_hooks.stamp_entries(mine, committer, 0, repo)
    ] == [OTHER]


def test_cmd_stamp_docs_outside_a_repository(plain_checkout: Path) -> None:
    assert documents_hooks.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    assert not list(plain_checkout.iterdir())


def test_cmd_stamp_docs_stamps_a_human_commit_from_receipts(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    stage_doc(repo, "docs/b.md")
    edit_by(MAIN, "docs/a.md", 1_800_000_000)
    assert documents_hooks.cmd_stamp_docs({}) == 0
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert staged_text(repo, "docs/b.md") == document().encode()
    stamped = front_of(staged_text(repo, "docs/a.md").decode())
    assert stamped["generated"] == [
        {
            "role": "generated",
            "harness": "claude-code",
            "model": "anthropic:claude-opus-5-5",
            "effort": "high",
            "surface": "cli",
            "session": SESSION_HASH,
            "at": STAMP_AT,
        }
    ]
    # Receipts are read, not consumed.
    assert len(receipts_of(repo, SESSION_HASH)) == 1


def test_cmd_stamp_docs_leaves_testdata_fixtures_byte_identical(repo: Path) -> None:
    """Regression: a staged fixture under testdata/ got a ``generated`` block."""
    rel = "tests/testdata/alpha/pull_request/passing-draft.md"
    stage_doc(repo, rel)
    before = (repo / rel).read_bytes()
    edit_by(MAIN, rel, 1_800_000_000)
    assert documents_hooks.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    assert staged_text(repo, rel) == before
    assert (repo / rel).read_bytes() == before


def test_cmd_stamp_docs_credits_a_shell_edit_to_the_committer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.write_record(
        SESSION_HASH, {"model": "claude-opus-5-5", "version": "2.1.316"}, repo
    )
    stage_doc(repo, "docs/a.md")
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    assert documents_hooks.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    (stamped,) = cast(
        "list[Entry]", front_of(staged_text(repo, "docs/a.md").decode())["generated"]
    )
    assert stamped["at"] == STAMP_AT
    assert stamped["harness"] == "claude-code:2.1.316"
    assert stamped["session"] == SESSION_HASH


def test_cmd_stamp_docs_scans_codex_rollouts(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, cwd=str(repo)),
            turn("gpt-5.6-sol", "low"),
            patch_call("docs/a.md"),
        ],
    )
    stage_doc(repo, "docs/a.md")
    env = {"CODEX_HOME": str(home), "CODEX_SESSION_ID": ROOT_THREAD}
    assert documents_hooks.cmd_stamp_docs(env) == 0
    (stamped,) = cast(
        "list[Entry]", front_of(staged_text(repo, "docs/a.md").decode())["generated"]
    )
    assert stamped["harness"] == "codex"
    assert stamped["model"] == "openai:gpt-5.6-sol"
    assert stamped["effort"] == "low"
    assert stamped["session"] == records.sha256_64(ROOT_THREAD)


def test_stamp_entries_reads_the_root_record_from_the_given_base(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.write_record(
        SESSION_HASH, {"model": "claude-opus-5-5", "version": "3.1"}, repo
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    child = dataclasses.replace(
        WRITER, root=SESSION_HASH, model="anthropic:claude-haiku-5-5"
    )
    got = documents_hooks.stamp_entries(
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
    got = documents_hooks.delegating_root(WRITER, None, repo)
    assert got is not None
    assert got.model == "anthropic:" + OPUS
