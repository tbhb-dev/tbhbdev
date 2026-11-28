"""Run index fields and status are derived from supplied values."""

import pytest
from test_alpha_agent_request import BUILD

from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.run_host.index import IndexRun, index_entry


@pytest.mark.parametrize("rc", [None, 0, 7])
def test_index_entry_contains_the_run_and_outcome(rc: int | None) -> None:
    value = request.parse(
        BUILD | {"unsandboxed": True, "authorized_by": "example/repo#1"}
    )
    run = IndexRun(value, "sample", "example", 10.0, "/repo", "primary")
    ended = None if rc is None else 11.0
    entry = index_entry(run, rc, ended=ended)
    assert entry["run"] == "sample"
    assert entry["host"] == "example"
    assert entry["worktree"] == "/repo"
    assert entry["repo"] == value.repo
    assert entry["started"] == 10.0
    assert entry["ended"] == ended
    assert entry["rc"] == rc
    assert entry["claude_profile"] == "primary"
    assert entry["state"] == ("running" if rc is None else "ended")
