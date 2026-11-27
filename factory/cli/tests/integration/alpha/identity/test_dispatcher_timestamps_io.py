# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Timestamp attribution through the commit message hook."""

from typing import TYPE_CHECKING

import pytest
import test_alpha_identity as support

from tbhbdev.alpha.identity import dispatcher as ai
from tbhbdev.alpha.identity import trailers

if TYPE_CHECKING:
    from pathlib import Path

fake_bin = support.fake_bin
isolated = support.isolated
repo = support.repo


@pytest.mark.parametrize("branch", ["topic", "initial", "detached"])
def test_session_commit_start_from_branch_reflog_or_session(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
) -> None:
    support.stage(repo, "base")
    support.commit(repo)
    if branch == "topic":
        with monkeypatch.context() as dated:
            dated.setenv("GIT_COMMITTER_DATE", support.EARLY)
            support.run_git(repo, "switch", "-qc", "topic")
        support.stage(repo, "later")
        support.commit(repo)
    elif branch == "detached":
        support.run_git(repo, "switch", "--detach", "HEAD")
    session = ai.sha256_64(support.CLAUDE_COMMIT_ENV["CLAUDE_CODE_SESSION_ID"])
    ai.write_record(session, {"model": "anthropic:m", "started_at": support.LATE})
    support.stage(repo, "change")
    path = support.msg_file(tmp_path)
    assert ai.cmd_trailer(str(path), "message", support.CLAUDE_COMMIT_ENV) == 0
    [(_, trailer)] = ai.llm_trailers(path.read_text())
    assert trailers.trailer_start(trailer) == (
        support.EARLY if branch == "topic" else support.LATE
    )
