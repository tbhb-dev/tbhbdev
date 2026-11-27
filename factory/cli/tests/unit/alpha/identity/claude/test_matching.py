# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity claude matching."""

from __future__ import annotations

import pytest

from tbhbdev.alpha.identity import records
from tbhbdev.alpha.identity.claude import matching as claude_matching
from tbhbdev.alpha.identity.commands import cmd_hashes
from tbhbdev.alpha.identity.records import AttributionError, Json


def test_defaults_for_missing_fields() -> None:
    assert records.rec_ts({}) == 0
    assert records.rec_ts({"ts": 5}) == 5
    assert claude_matching.rec_hashes({}) == set()
    assert claude_matching.rec_hashes({"cmd": ["a", "b"]}) == {"a", "b"}


def test_matching_prefers_an_exact_agent_and_refuses_an_unresolved_child() -> None:
    records: list[Json] = [
        {"agent": "child", "cmd": cmd_hashes("git commit -m test")},
        {"agent": None, "cmd": cmd_hashes("git status")},
    ]
    assert (
        claude_matching.agent_for_commands(records, ["sh -c eval 'git commit -m test'"])
        == "child"
    )
    assert (
        claude_matching.agent_for_commands(records, ["sh -c eval 'git status'"]) is None
    )
    with pytest.raises(AttributionError, match="Attribution couldn't be resolved"):
        claude_matching.agent_for_commands(records, ["sh -c eval 'git add file'"])
