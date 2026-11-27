# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity commit inputs."""

from __future__ import annotations

import json
import os
lazy from pathlib import Path

from unit.alpha.identity.identity_examples import OTHER, SESSION_HASH
from unit.alpha.identity.identity_fixtures import AGENT, run_git, stage
lazy import pytest

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import models, sessions
from tbhbdev.alpha.identity.commit import inputs as commit_inputs


def committer_of(
    who: models.Ident, root: models.Ident | None = None
) -> models.Committer:
    return models.Committer(models.CLAUDE, who, root or who)


def test_staged_info(repo: Path, plain_checkout: Path) -> None:
    assert commit_inputs.staged_info(repo) == ([], {})
    stage(repo, "a.txt")
    stage(repo, "dir/b c.txt")
    paths, blobs = commit_inputs.staged_info(repo)
    assert sorted(paths) == ["a.txt", "dir/b c.txt"]
    assert blobs["a.txt"] == run_git(repo, "hash-object", "a.txt").strip()
    assert set(blobs) == set(paths)
    assert commit_inputs.staged_info(plain_checkout) == ([], {})


def test_staged_info_skips_unmerged_entries(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = {
        "diff": (0, "conflict.txt\x00ok.txt\x00"),
        "ls-files": (
            0,
            "100644 abc 1\tconflict.txt\x00100644 def 0\tok.txt\x00bad entry\x00",
        ),
    }

    def fake(*args: str, **_kwargs: Path | None) -> tuple[int, str]:
        return outputs[args[0]]

    monkeypatch.setattr(git_repo, "git", fake)
    assert commit_inputs.staged_info(repo) == (
        ["conflict.txt", "ok.txt"],
        {"ok.txt": "def"},
    )


def test_enrich_child_model_and_effort_from_its_record(repo: Path) -> None:
    sessions.write_record(
        AGENT,
        {"root": SESSION_HASH, "model": "claude-haiku-5-5", "effort": "medium"},
        repo,
    )
    cm = committer_of(models.Ident(SESSION_HASH, "claude-code:2.1"))
    child = models.Ident(SESSION_HASH, "claude-code", AGENT)
    got = commit_inputs.enrich(child, cm)
    assert (got.model, got.effort, got.harness) == (
        "anthropic:claude-haiku-5-5",
        "medium",
        "claude-code:2.1",
    )
    # Existing values win over the record.
    full = models.Ident(SESSION_HASH, "claude-code", AGENT, "anthropic:x", "low")
    assert commit_inputs.enrich(full, cm) == replace_harness(full, "claude-code:2.1")
    half = models.Ident(SESSION_HASH, "claude-code", AGENT, "anthropic:x")
    got = commit_inputs.enrich(half, cm)
    assert (got.model, got.effort) == ("anthropic:x", "medium")
    half = models.Ident(SESSION_HASH, "claude-code", AGENT, None, "low")
    got = commit_inputs.enrich(half, cm)
    assert (got.model, got.effort) == ("anthropic:claude-haiku-5-5", "low")


def replace_harness(ident: models.Ident, harness: str) -> models.Ident:
    return models.Ident(
        ident.root, harness, ident.subagent, ident.model, ident.effort, ident.surface
    )


def test_enrich_child_ignores_records_it_cannot_trust(repo: Path) -> None:
    sessions.write_record(
        AGENT, {"root": OTHER, "model": "claude-haiku-5-5", "effort": "medium"}, repo
    )
    cm = committer_of(models.Ident(SESSION_HASH, "claude-code"))
    child = models.Ident(SESSION_HASH, "claude-code", AGENT)
    assert commit_inputs.enrich(child, cm) == child
    sessions.write_record(
        AGENT, {"root": SESSION_HASH, "model": "claude-haiku-5-5"}, repo
    )
    codex_cm = models.Committer(
        models.CODEX,
        models.Ident(SESSION_HASH, "codex"),
        models.Ident(SESSION_HASH, "codex"),
    )
    assert commit_inputs.enrich(child, codex_cm) == child
    # A child of the main agent (no subagent) is never looked up.
    assert commit_inputs.enrich(
        models.Ident(SESSION_HASH, "claude-code"), cm
    ) == models.Ident(SESSION_HASH, "claude-code")


def test_enrich_child_reads_the_transcript(repo: Path) -> None:
    transcript = repo / "t.jsonl"
    transcript.write_text(
        json.dumps({"message": {"model": "claude-haiku-5-5"}}) + "\n", encoding="utf-8"
    )
    sessions.write_record(
        AGENT, {"root": SESSION_HASH, "transcript": str(transcript)}, repo
    )
    cm = committer_of(models.Ident(SESSION_HASH, "claude-code"))
    got = commit_inputs.enrich(models.Ident(SESSION_HASH, "claude-code", AGENT), cm)
    assert got.model == "anthropic:claude-haiku-5-5"
    assert got.effort is None


def test_unstaged_blobs_covers_only_staged_files_that_differ(repo: Path) -> None:
    stage(repo, "same.txt")
    stage(repo, "changed.txt")
    stage(repo, "gone.txt")
    stage(repo, "other.txt")
    (repo / "changed.txt").write_text("later\n", encoding="utf-8")
    (repo / "other.txt").write_text("later\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    later = run_git(repo, "hash-object", "changed.txt").strip()
    staged = {"same.txt", "changed.txt", "gone.txt"}
    assert commit_inputs.unstaged_blobs(repo, staged) == {"changed.txt": later}


def test_staged_info_edge_paths(repo: Path) -> None:
    stage(repo, "committed.txt")
    run_git(repo, "commit", "-q", "--no-verify", "-m", "c")
    (repo / "committed.txt").write_text("changed\n", encoding="utf-8")
    stage(repo, "-dash.txt")
    stage(repo, "tab\there.txt")
    paths, blobs = commit_inputs.staged_info(repo)
    assert sorted(paths) == ["-dash.txt", "tab\there.txt"]
    assert sorted(blobs) == ["-dash.txt", "tab\there.txt"]


def test_staged_info_defaults_to_the_process_cwd(repo: Path) -> None:
    stage(repo, "top.txt")
    os.chdir(repo)
    paths, blobs = commit_inputs.staged_info()
    assert (paths, sorted(blobs)) == (["top.txt"], ["top.txt"])
