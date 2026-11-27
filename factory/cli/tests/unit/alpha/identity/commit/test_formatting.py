# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for commit trailer formatting with recorded start times."""

from tbhbdev.alpha.identity.commit.formatting import apply_record, should_merge_message

VALUE = "LLM harness=codex;model=openai:m;started_at=2026-11-01T10:00:00Z"


def test_record_start_replaces_the_message_value() -> None:
    start = "2026-11-02T10:00:00Z"
    assert apply_record(VALUE, {}, {"started_at": start}, None) == (
        f"LLM harness=codex;model=openai:m;started_at={start}"
    )
    assert apply_record(VALUE, {}, {}, None) == VALUE


def test_squash_comment_markers_follow_git_configuration() -> None:
    text = f"subject\n\nGenerated-by: {VALUE}\n! This is a combination of 2 commits.\n"
    assert should_merge_message(text, [], "auto")
    assert not should_merge_message(text, [], "#")
