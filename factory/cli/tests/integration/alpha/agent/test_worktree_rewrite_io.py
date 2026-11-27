"""Rebase and publish a start-ref build from a fresh run clone."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from agent_host_example import (
    commit,
    git,
    isolate_host_git,
    local_mirror_fetches,
    repository,
    run_request,
)
from fake_tool import pythonpath, write_fake

from tbhbdev.alpha import pre_push_guard, run_stop_hook
from tbhbdev.alpha.agent import push, runfiles, worktree


@pytest.fixture
def run_tools(
    isolated_agent_home: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del isolated_agent_home
    resolve = worktree.host_tools.resolve

    def executable(name: str) -> str:
        return "/unused/prek" if name == "prek" else resolve(name)

    monkeypatch.setattr(worktree.host_tools, "resolve", executable)
    binary = tmp_path / "bin"
    binary.mkdir()
    write_fake(
        binary / "fake-agent-cli",
        "import sys\n"
        "from tbhbdev.alpha.pre_push_guard import main\n"
        "if sys.argv[1:3] == ['alpha', 'identity']:\n"
        "    sys.exit(0)\n"
        "assert sys.argv[1:3] == ['alpha', 'pre-push-guard']\n"
        "sys.exit(main(sys.argv[3:], 'pre-push-guard'))\n",
    )
    (binary / "tbhbdev").symlink_to("fake-agent-cli")
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(
        "PYTHONPATH", pythonpath(Path(pre_push_guard.__file__).parents[2])
    )
    # Keep the installed hook's interpreter path within the shebang limit.
    prefix = tmp_path / "python"
    prefix.symlink_to(Path(sys.base_prefix), target_is_directory=True)
    monkeypatch.setattr(sys, "base_prefix", str(prefix))


@pytest.mark.usefixtures("run_tools")
@pytest.mark.parametrize(
    ("remote_state", "error"),
    [
        ("current", ""),
        ("advanced", "stale info"),
        ("missing-fetch", "the remote tip is not the fetched one"),
    ],
)
def test_start_ref_rebase_and_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_state: str, error: str
) -> None:
    seed, clone = repository(tmp_path)
    remote = tmp_path / "origin"
    branch = "refs/heads/fix/topic"
    tracking = "refs/remotes/origin/fetched/heads/fix/topic"
    git(seed, "switch", "-c", "fix/topic")
    (seed / "topic").write_text("topic change\n")
    start = commit(seed, "topic")
    git(seed, "push", "origin", branch)
    git(seed, "switch", "main")
    upstream = commit(seed, "upstream")
    git(seed, "push", "origin", "main")

    isolate_host_git(tmp_path, monkeypatch)
    local_mirror_fetches(monkeypatch, remote)
    request = run_request(start_ref=branch, fetch_refs=[branch])
    workspace = worktree.create(clone, "example", request)
    assert git(workspace, "for-each-ref", "refs/remotes/origin/fix/topic") == ""
    expected = git(workspace, "rev-parse", "--verify", tracking)
    assert expected == start
    git(workspace, "config", "remote.origin.pushurl", str(remote))

    files = runfiles.RunFiles(tmp_path / "state", "example")
    stop = run_stop_hook.run_context(request, str(workspace), files, tmp_path, start)
    assert run_stop_hook.build_reason(stop) is not None
    before, _ = push.local_refs(workspace)
    git(
        workspace,
        "-c", "user.name=Example",
        "-c", "user.email=example@example.test",
        "-c", "commit.gpgsign=false",
        "rebase", "origin/main",
    )  # fmt: skip
    assert git(workspace, "rev-parse", "HEAD^") == upstream
    rewritten = git(workspace, "rev-parse", "HEAD")
    after, checkout = push.local_refs(workspace)
    assert push.new_commits(
        Mock(request=request, files=files), workspace, before, after, checkout
    ) == [("refs/heads/agent/example", rewritten)]

    remote_tip = start
    if remote_state == "advanced":
        git(seed, "switch", "fix/topic")
        remote_tip = commit(seed, "concurrent topic change")
        git(seed, "push", "origin", branch)
    elif remote_state == "missing-fetch":
        git(workspace, "update-ref", "-d", tracking)

    def publish(tip: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "push",
                f"--force-with-lease={branch}:{tip}",
                "origin",
                f"HEAD:{branch}",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    result = publish(expected)
    assert bool(result.returncode) == bool(error), result.stderr
    assert (error or f"{branch} rewritten {start} -> {rewritten}") in result.stderr
    if not error:
        assert run_stop_hook.build_reason(stop) is None
        remote_tip = rewritten
    elif remote_state == "advanced":
        result = publish(remote_tip)
        assert result.returncode != 0
        assert "remote tip changed since fetch" in result.stderr
    assert git(remote, "rev-parse", branch) == remote_tip
