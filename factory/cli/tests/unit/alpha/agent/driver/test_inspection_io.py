"""Exercise coordinator inspection through fake tools and run records."""

from __future__ import annotations

import pytest
from agent_driver_example import (
    Remote,
    git_in,
    mirror_in,
    write_standalone_fake,
)
from driver_scenarios import (
    COMMIT,
    CONFIG,
    command,
    launch,
)

from tbhbdev.alpha.agent.driver import (
    metadata,
)


@pytest.mark.parametrize(("verb", "profile"), [("log", "build"), ("diff", "review")])
def test_unsandboxed_inspection_reads_the_host_output(
    remote: Remote, capsys: pytest.CaptureFixture[str], verb: str, profile: str
) -> None:
    options = ["--ref", COMMIT] if profile == "review" else []
    assert (
        launch(
            remote, "--ops", "--unsandboxed", "o/r#1", "--profile", profile, *options
        )
        == 0
    )
    remote.file(".out.final", "verdict text\n")
    capsys.readouterr()
    assert command(verb) == 0
    assert "verdict text" in capsys.readouterr().out
    if verb == "diff":
        assert command("diff", "--patch") == 1
        assert "diff --patch requires a finished build run" in capsys.readouterr().err


@pytest.mark.parametrize("unsandboxed", [False, True])
def test_timed_out_log_reads_the_run_log_without_git(
    remote: Remote, capsys: pytest.CaptureFixture[str], unsandboxed: bool
) -> None:
    options = ["--unsandboxed", "o/r#1"] if unsandboxed else []
    assert launch(remote, *options) == 0
    remote.file(".rc", "124\n")
    remote.file(".log", "harness exited 124\n")
    remote.file(".out.final", "partial answer\n")
    capsys.readouterr()
    assert command("log") == 0
    assert capsys.readouterr() == ("harness exited 124\n", "")
    assert remote.records("git") == []


@pytest.mark.parametrize("ops", [False, True])
def test_unsandboxed_build_diff_inspects_commits(
    remote: Remote, capsys: pytest.CaptureFixture[str], ops: bool
) -> None:
    options = ["--ops"] if ops else []
    assert launch(remote, "--unsandboxed", "o/r#1", *options) == 0
    remote.file(".out.final", "report without patches\n")
    capsys.readouterr()
    assert command("diff") == 0
    assert capsys.readouterr() == ("diff output\n", "")
    call = remote.records("ssh")[-1]["args"]
    assert isinstance(call, list)
    assert call[call.index("-l") + 1 : -1] == [
        metadata.load(CONFIG, "sample").host.account,
        "example-linux",
    ]


@pytest.mark.parametrize("verb", ["log", "diff"])
def test_inspection_uses_recorded_target_and_mirror(
    remote: Remote, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    assert command(verb) == 0
    assert capsys.readouterr() == (verb + " output\n", "")
    calls = [record["args"] for record in remote.records("git")]
    if verb == "diff":
        query = calls.pop(0)
        assert isinstance(query, list)
        assert "ls-remote" in query
    assert len(calls) == 2
    assert isinstance(calls[0], list)
    assert isinstance(calls[1], list)
    assert "fetch" in calls[0]
    assert verb in calls[1]
    assert "--no-optional-locks" in calls[1]
    if verb == "log":
        assert calls[1][calls[1].index("-n") + 1] == "10"
    else:
        assert "-n" not in calls[1]
    assert len(remote.records("ssh")) == 5

    mirror_path = (
        remote.root
        / "state/tbhbdev/agent-mirrors/github.com/example-org/example-repo.git"
    )
    mirror_path.rename(remote.root / "unused-mirror")
    mirror = mirror_in(remote.root)
    clone = (
        remote.root
        / "home/Code/github.com/example-org/example-repo/.worktrees/agent-sample"
    )
    git_in(clone, "init")
    marker = remote.root / "clone-origin-executed"
    git_in(clone, "remote", "add", "origin", f"ext::touch {marker}")
    write_standalone_fake(
        remote.bin / "git",
        "import os, sys\nos.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n",
    )
    assert command(verb) == 0
    output = capsys.readouterr()
    assert "pushed change" in output.out
    assert output.err == ""
    assert not marker.exists()
    assert git_in(mirror, "rev-parse", "refs/heads/agent/sample") == git_in(
        remote.root / "source", "rev-parse", "agent/sample"
    )
    git_in(mirror, "config", "remote.origin.url", str(remote.root / "missing.git"))
    assert command(verb) == 1


@pytest.mark.parametrize("verb", ["log", "diff"])
def test_inspection_filters_terminal_controls(
    remote: Remote, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(**{verb: "é\tline\n\x1b\x07\x00\x7f\x85\u200e"})
    assert command(verb) == 0
    assert capsys.readouterr() == ("é\tline\n", "")


def test_log_count(remote: Remote) -> None:
    assert launch(remote) == 0
    assert command("log", "-n", "2") == 0
    args = remote.records("git")[-1]["args"]
    assert isinstance(args, list)
    assert args[args.index("-n") + 1] == "2"


@pytest.mark.parametrize("verb", ["log", "diff"])
def test_inspection_fetch_failure_stops_before_read(remote: Remote, verb: str) -> None:
    assert launch(remote) == 0
    remote.configure(fetch_error=74)
    assert command(verb) == 1
    calls = remote.records("git")
    assert len(calls) == (2 if verb == "diff" else 1)
    args = calls[-1]["args"]
    assert isinstance(args, list)
    assert "fetch" in args
