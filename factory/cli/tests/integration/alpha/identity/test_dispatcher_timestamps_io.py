# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Timestamp attribution through the commit message hook."""

from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.identity import records as identity_records
from tbhbdev.alpha.identity import sessions as identity_sessions
from tbhbdev.alpha.identity import trailers
from tbhbdev.alpha.identity.commit import hooks as identity_commit_hooks
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from pathlib import Path

EARLY = "2026-11-14T00:02:41Z"
LATE = "2026-11-14T00:09:00Z"


@pytest.mark.usefixtures("isolated_home", "pr_checkout")
@pytest.mark.parametrize("branch", ["topic", "initial", "detached"])
def test_session_commit_start_from_branch_reflog_or_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    if branch == "topic":
        with monkeypatch.context() as dated:
            dated.setenv("GIT_COMMITTER_DATE", EARLY)
            assert git("switch", "-qc", "topic")[0] == 0
        assert git("commit", "--allow-empty", "-qm", "later")[0] == 0
    elif branch == "detached":
        assert git("switch", "--detach", "HEAD")[0] == 0
    env = {"CLAUDE_CODE_SESSION_ID": "session-raw"}
    identity_sessions.write_record(
        identity_records.sha256_64("session-raw"),
        {"model": "anthropic:m", "started_at": LATE},
    )
    path = tmp_path / "MSG"
    path.write_text("subject\n")
    assert identity_commit_hooks.cmd_trailer(str(path), "message", env) == 0
    [(_, trailer)] = trailers.llm_trailers(path.read_text())
    assert trailers.trailer_start(trailer) == (EARLY if branch == "topic" else LATE)
