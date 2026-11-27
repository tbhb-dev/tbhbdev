# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Retain the oldest trailer timestamp when creating or editing a pull request."""

import json

import test_alpha_pull_request as support

from tbhbdev.alpha import pull_request as pr

agent = support.agent
world = support.world


def test_create_keeps_earliest_start(world: support.World) -> None:
    early = support.value(
        harness="codex", model="openai:m", started_at="2026-11-14T00:02:41Z"
    )
    late = early.replace("00:02:41", "00:09:00")
    world.commit(f"one\n\nGenerated-by: {late}")
    world.commit(f"two\n\nAssisted-by: {early}")
    support.write_draft(world)
    assert support.run_main("create") == 0
    _, call = world.calls("gh")
    assert str(call["stdin"]).endswith(f"Generated-by: {early}\n")


def test_edit_keeps_earliest_start_from_commit_trailers(
    world: support.World, agent: dict[str, str]
) -> None:
    local = (
        "LLM effort=medium;harness=codex:0.171.0;model=openai:gpt-6.2-sol;"
        "session=sha-256-64:ae40cbadd8ab72ec;surface=cli"
    )
    attrs = (
        "effort=medium;harness=claude-code;model=anthropic:claude-sonnet-5-5;"
        "session=sha-256-64:7ff1a5b545107a55;surface=cli"
    )
    early = attrs.replace(";surface=", ";started_at=2026-11-14T00:02:41Z;surface=")
    late = attrs.replace(";surface=", ";started_at=2026-11-14T00:09:00Z;surface=")
    merged = early.replace("7ff1a5b545107a55", "1234567890abcdef")
    world.git("switch", "-q", "-c", "other-branch")
    world.commit(f"other PR\n\nGenerated-by: {local}")
    pages = [
        [{"commit": {"message": f"one\n\nGenerated-by: LLM {late}"}}],
        [
            {"commit": {"message": f"two\n\nAssisted-by: LLM {early}"}},
            {
                "parents": [{"sha": "a" * 40}, {"sha": "b" * 40}],
                "commit": {"message": f"merge\n\n---\n\nGenerated-by: LLM\n {merged}"},
            },
            {"commit": {"message": f"body\n\nGenerated-by: {local}\n\nMore prose."}},
        ],
    ]
    argv = support.fake_pr_commits(world, 40, json.dumps(pages))
    support.write_draft(world)
    assert support.run_main("edit", str(pr.DRAFT), "--pr", "40", env=agent) == 0
    _, commits, edit = world.calls("gh")
    assert commits["argv"] == argv
    assert str(edit["stdin"]).split("\n\n")[-1] == (
        f"Generated-by: LLM {early}\nGenerated-by: LLM {merged}\n"
        f"Assisted-by: {support.POSTING}\n"
    )
    (scan,) = world.calls("gitleaks")
    assert scan["stdin"] == f"fix: x\n\n{edit['stdin']}"
