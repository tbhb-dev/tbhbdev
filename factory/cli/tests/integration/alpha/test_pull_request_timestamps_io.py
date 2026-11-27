# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Retain the oldest trailer timestamp when creating or editing a pull request."""

import json

import test_alpha_pull_request as support

world = support.world


def timestamps() -> tuple[str, str]:
    early = support.value(
        harness="codex", model="openai:m", started_at="2026-11-14T00:02:41Z"
    )
    late = early.replace("00:02:41", "00:09:00")
    return early, late


def test_create_keeps_earliest_start(world: support.World) -> None:
    early, late = timestamps()
    world.commit(f"one\n\nGenerated-by: {late}")
    world.commit(f"two\n\nAssisted-by: {early}")
    support.write_draft(world)
    assert support.run_main("create") == 0
    _, call = world.calls("gh")
    assert str(call["stdin"]).endswith(f"Generated-by: {early}\n")


def test_edit_keeps_earliest_start_from_commit_trailers(world: support.World) -> None:
    early, late = timestamps()
    pages = [
        [{"commit": {"message": f"one\n\nGenerated-by: {late}"}}],
        [{"commit": {"message": f"two\n\nAssisted-by: {early}"}}],
    ]
    support.fake_pr_commits(world, 40, json.dumps(pages))
    draft = support.write_draft(world)
    assert support.run_main("edit", str(draft), "--pr", "40") == 0
    _, _, call = world.calls("gh")
    assert str(call["stdin"]).endswith(f"Generated-by: {early}\n")
