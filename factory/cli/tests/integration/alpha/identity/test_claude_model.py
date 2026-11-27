# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity claude model tests."""

import json
import os
import time
from typing import TYPE_CHECKING

import pytest
from integration.alpha.identity.identity_test_support import (
    CLAUDE_COMMIT_ENV,
    EARLIER,
    EARLIER_TS,
    HAIKU,
    OPUS,
    SONNET,
    Json,
    reply,
    write_transcript,
)

from tbhbdev.alpha.identity import attribution, claude_commands, claude_model

if TYPE_CHECKING:
    from pathlib import Path


def test_transcript_model_reads_the_last_real_model(tmp_path: Path) -> None:
    path = write_transcript(
        tmp_path / "t.jsonl",
        {"type": "user", "message": {"content": "x"}},
        {"type": "assistant", "message": {"model": "claude-sonnet-5-5"}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},
        {"type": "assistant", "message": {"model": ""}},
        {"type": "assistant", "message": "not an object"},
        {"type": "system"},
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    assert claude_model.transcript_model(str(path)) == "claude-sonnet-5-5"
    assert claude_model.transcript_model(str(tmp_path / "missing")) is None
    assert claude_model.transcript_model(None) is None
    assert claude_model.transcript_model(3) is None
    only = write_transcript(tmp_path / "o.jsonl", {"message": {"model": "<x>"}})
    assert claude_model.transcript_model(str(only)) is None


def test_transcript_model_only_reads_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_transcript(
        tmp_path / "t.jsonl",
        {"message": {"model": "old"}},
        {"message": {"model": "new"}},
    )
    monkeypatch.setattr(claude_model, "CLAUDE_TAIL", 40)
    assert claude_model.transcript_model(str(path)) == "new"
    path.write_text(
        json.dumps({"message": {"model": "old"}}) + "\n" + "x" * 100 + "\n",
        encoding="utf-8",
    )
    assert claude_model.transcript_model(str(path)) is None


def test_real_model() -> None:
    assert claude_model.real_model(OPUS) == OPUS
    assert claude_model.real_model("<synthetic>") is None
    assert claude_model.real_model("<other>") is None
    assert claude_model.real_model("") is None
    assert claude_model.real_model(None) is None
    assert claude_model.real_model(3) is None


def test_entry_time() -> None:
    assert claude_model.entry_time(EARLIER) == EARLIER_TS
    assert claude_model.entry_time("2026-11-06T10:00:00.500+00:00") == EARLIER_TS + 0.5
    assert claude_model.entry_time(None) == 0.0
    assert claude_model.entry_time("yesterday") == 0.0
    assert claude_model.entry_time("") == 0.0


def test_transcript_entry_reads_the_last_real_reply_and_its_time(
    tmp_path: Path,
) -> None:
    path = write_transcript(
        tmp_path / "t.jsonl",
        reply(OPUS, "2026-11-06T09:00:00Z"),
        reply(SONNET),
        reply("<synthetic>", "2026-11-06T11:00:00Z"),
        {"type": "system", "timestamp": "2026-11-06T12:00:00Z"},
    )
    assert claude_model.transcript_entry(str(path)) == (SONNET, EARLIER_TS)
    no_time = write_transcript(tmp_path / "u.jsonl", reply(OPUS, None))
    assert claude_model.transcript_entry(str(no_time)) == (OPUS, 0.0)
    assert claude_model.transcript_entry(str(tmp_path / "missing")) is None
    assert claude_model.transcript_entry(None) is None
    empty = write_transcript(tmp_path / "e.jsonl", {"type": "user"})
    assert claude_model.transcript_entry(str(empty)) is None


def test_main_model_order(tmp_path: Path) -> None:
    transcript = str(write_transcript(tmp_path / "t.jsonl", reply(OPUS)))
    switch: Json = {"switch_model": SONNET, "switch_at": EARLIER_TS + 1}
    # The newer of the switch and the transcript's last reply.
    assert claude_model.main_model({"transcript": transcript, **switch}) == SONNET
    older = {"switch_model": SONNET, "switch_at": EARLIER_TS - 1}
    assert claude_model.main_model({"transcript": transcript, **older}) == OPUS
    tie = {"switch_model": SONNET, "switch_at": EARLIER_TS}
    assert claude_model.main_model({"transcript": transcript, **tie}) == OPUS
    # A switch without a usable time doesn't count.
    assert (
        claude_model.main_model({"transcript": transcript, "switch_model": SONNET})
        == OPUS
    )
    int_time = {"switch_model": SONNET, "switch_at": 1}
    assert claude_model.main_model({"transcript": transcript, **int_time}) == OPUS
    # A reply without a readable time is older than any switch.
    bare = str(write_transcript(tmp_path / "b.jsonl", reply(OPUS, None)))
    assert claude_model.main_model({"transcript": bare, **switch}) == SONNET
    # Only a switch, then the transcript, then what hooks recorded.
    assert claude_model.main_model(switch) == SONNET
    assert claude_model.main_model({"transcript": transcript, "model": HAIKU}) == OPUS
    gone = str(tmp_path / "gone.jsonl")
    hooks: Json = {"transcript": gone, "model": HAIKU, "payload_model": SONNET}
    assert claude_model.main_model(hooks) == HAIKU
    assert (
        claude_model.main_model({"transcript": gone, "payload_model": SONNET}) == SONNET
    )
    assert claude_model.main_model({"transcript": gone}) is None
    assert claude_model.main_model({}) is None


def finished(stop: str | None = "end_turn") -> Json:
    return {"type": "assistant", "message": {"model": HAIKU, "stop_reason": stop}}


@pytest.mark.usefixtures("repo")
def test_a_running_subagent_refuses_a_commit_without_hook_records(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    project = config / "projects" / "example-project"
    write_transcript(project / "session-raw.jsonl", reply(SONNET))
    env = {**CLAUDE_COMMIT_ENV, "CLAUDE_CONFIG_DIR": str(config)}
    folder = project / "session-raw" / "subagents"
    now = time.time()
    # No subagent, then only finished ones: the main model is right.
    assert not claude_model.subagent_running("session-raw", env, now)
    done = write_transcript(folder / "agent-a.jsonl", {"type": "user"}, finished())
    write_transcript(folder / "agent-b.jsonl", {"type": "api-request"})
    assert not claude_model.subagent_running("session-raw", env, now)
    assert attribution.committer_identity(env, []) is not None
    # An old unfinished transcript is abandoned work, not a running agent.
    stale = write_transcript(folder / "agent-c.jsonl", {"type": "user"})
    os.utime(stale, (now - claude_commands.CMD_WINDOW - 1,) * 2)
    assert not claude_model.subagent_running("session-raw", env, now)
    # A recent transcript that doesn't end its turn means a subagent is working.
    unfinished: tuple[Json, ...] = (finished("tool_use"), {"type": "user"})
    for last in unfinished:
        write_transcript(done, last)
        assert claude_model.subagent_running("session-raw", env, now)
        with pytest.raises(
            claude_commands.AttributionError, match="subagent of this session"
        ):
            attribution.committer_identity(env, [])
    # A subagent the user stopped isn't running, though its transcript ends mid-turn.
    write_transcript(done, finished("tool_use"))
    meta = folder / "agent-a.meta.json"
    meta.write_text(json.dumps({"stoppedByUser": True}))
    assert not claude_model.subagent_running("session-raw", env, now)
    assert attribution.committer_identity(env, []) is not None
    for text in ('{"stoppedByUser": false}', "[]", "not json"):
        meta.write_text(text)
        assert claude_model.subagent_running("session-raw", env, now)
    meta.unlink()
    write_transcript(done, finished())
    # A dangling link is skipped; a transcript that can't be read counts as unfinished.
    (folder / "agent-e.jsonl").symlink_to(folder / "gone")
    assert not claude_model.subagent_running("session-raw", env, now)
    unreadable = folder / "agent-d.jsonl"
    unreadable.mkdir()
    assert claude_model.subagent_running("session-raw", env, now)
    unreadable.rmdir()
    # An ID with glob characters or separators matches nothing.
    assert not claude_model.subagent_running("*", env, now)
    assert claude_model.session_model("../session-raw", env) is None
