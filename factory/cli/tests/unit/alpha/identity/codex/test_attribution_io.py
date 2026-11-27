# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity codex attribution."""

from __future__ import annotations

lazy from pathlib import Path

from unit.alpha.identity.identity_fixtures import (
    CHILD_THREAD,
    ROOT_THREAD,
    meta,
    turn,
    write_rollout,
)

from tbhbdev.alpha.identity import attribution, models, records


def test_codex_committer(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": CHILD_THREAD,
        "CODEX_VERSION": "0.99.0",
    }
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, source="exec"), turn("gpt-5.6-sol", "high")],
    )
    write_rollout(
        home,
        CHILD_THREAD,
        [meta(id=CHILD_THREAD, session_id=ROOT_THREAD), turn("gpt-5.6-luna", "low")],
    )
    got = attribution.committer_identity(env)
    assert got is not None
    assert got.key == models.CODEX
    assert got.ident == models.Ident(
        records.sha256_64(ROOT_THREAD),
        "codex:0.99.0",
        records.sha256_64(CHILD_THREAD),
        "openai:gpt-5.6-luna",
        "low",
        None,
    )
    assert got.root_ident == models.Ident(
        records.sha256_64(ROOT_THREAD),
        "codex:0.99.0",
        None,
        "openai:gpt-5.6-sol",
        "high",
        "exec",
    )
    solo = attribution.committer_identity(
        {k: v for k, v in env.items() if k != "CODEX_THREAD_ID"}
    )
    assert solo is not None
    assert solo.ident == solo.root_ident == got.root_ident


def test_codex_committer_without_rollouts(tmp_path: Path) -> None:
    env = {
        "CODEX_HOME": str(tmp_path),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": "",
    }
    got = attribution.committer_identity(env)
    assert got is not None
    assert got.ident == models.Ident(records.sha256_64(ROOT_THREAD), "codex")


def test_codex_committer_pins_the_thread_ids(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": CHILD_THREAD,
    }
    got = attribution.committer_identity(env)
    assert got is not None
    assert got.ident.subagent == records.sha256_64(CHILD_THREAD)
    assert got.root_ident.subagent is None
    # Rollout metadata naming some other thread never overrides the env.
    stray = "01a19ebf-0000-7000-8000-000000000000"
    write_rollout(home, ROOT_THREAD, [meta(id=stray)])
    write_rollout(home, CHILD_THREAD, [meta(id=stray)])
    again = attribution.committer_identity(env)
    assert again is not None
    assert again.ident.subagent == records.sha256_64(CHILD_THREAD)
    assert again.root_ident.subagent is None
