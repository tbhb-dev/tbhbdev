"""Exercise coordinator cleanup through fake tools and run records."""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import (
    Remote,
    git_in,
)
from driver_run_example import BUILD
from driver_scenarios import (
    PREPARE,
    command,
    launch,
    ticking,
)

from tbhbdev.alpha.agent import model, runfiles, transport
from tbhbdev.alpha.agent.decisions import request, runfiles_names
from tbhbdev.alpha.agent.driver import (
    cleanup,
    metadata,
)
from tbhbdev.alpha.agent.driver import launch as coordinator

if TYPE_CHECKING:
    from pathlib import Path


def test_unsandboxed_clean_refuses_a_run_without_a_result(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--account", "ops", "--unsandboxed", "o/r#1") == 0
    capsys.readouterr()
    assert command("clean") == 1
    assert "clean refused" in capsys.readouterr().err
    assert metadata.metadata_path("sample").exists()


@pytest.mark.parametrize("branch", [False, True])
def test_clean_refuses_unpushed_build_worktree(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    branch: bool,
) -> None:
    monkeypatch.setattr(coordinator, "prepare", PREPARE)
    assert launch(remote) == 0
    remote.configure(session=False, branch=branch)
    tree = remote.root / (
        "home/Code/github.com/example-org/example-repo/.worktrees/agent-sample"
    )
    git_in(tree, "init", "-b", "agent/sample")
    git_in(
        tree,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "unpublished change",
    )
    commit = git_in(tree, "rev-parse", "HEAD")
    remote.file(".rc", "0")
    remote.file(".out.final", "unread output\n")
    path = metadata.metadata_path("sample")
    before = path.read_bytes()
    capsys.readouterr()
    assert command("clean") == 1
    assert capsys.readouterr().err == (
        "tbhbdev alpha agent: full-access run may have unpublished commits or unread "
        "output; pass --force to clean anyway\n"
    )
    assert path.read_bytes() == before
    assert git_in(tree, "rev-parse", "HEAD") == commit
    assert (remote.state / "sample.out.final").read_text() == "unread output\n"
    assert (remote.state / "sample.owner").is_file()
    assert not (remote.state / "trash").exists()
    assert remote.records("git") == []
    assert command("clean", "--force") == 0
    assert not path.exists()
    trash = next((remote.state / "trash").iterdir())
    assert (trash / "worktree").is_dir()
    assert git_in(trash / "worktree", "rev-parse", "HEAD") == commit
    assert (trash / "sample.out.final").read_text() == "unread output\n"


def test_unsandboxed_force_clean_of_an_ended_run_only_moves_to_trash(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--account", "ops", "--unsandboxed", "o/r#1") == 0
    remote.configure(session=False)
    remote.file(".rc", "0")
    before = {tool: remote.records(tool) for tool in ("ssh", "git", "tmux")}
    capsys.readouterr()
    assert command("clean", "--force") == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert remote.records("git") == before["git"]
    assert len(remote.records("tmux")) == len(before["tmux"]) + 2
    calls = remote.records("ssh")
    assert len(calls) == len(before["ssh"]) + 2
    script = str(calls[-1]["args"])
    assert "agent-mirrors" not in script
    assert "tmux_state has-session ag-sample" in script
    assert " git " not in script
    assert not metadata.metadata_path("sample").exists()
    trash = next((remote.state / "trash").iterdir())
    assert trash.name.endswith("-sample")
    assert (trash / "worktree").is_dir()
    assert (trash / "sample.owner").is_file()
    assert set(output.out.splitlines()) == {
        "moved sample.json",
        *(f"moved {path.name}" for path in trash.iterdir()),
    }


def test_clean_running(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    assert command("clean", "--force") == 1
    assert "clean refused" in capsys.readouterr().err
    assert remote.records("git") == []


def test_clean(remote: Remote) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.file(".rc", "0")
    assert command("clean", "--force") == 0
    assert not (remote.local / "tbhbdev/agent-runs/sample.json").exists()
    assert list((remote.local / "tbhbdev/agent-runs/trash").glob("*/sample.json"))
    assert not (remote.state / "sample.request.json").exists()
    assert list((remote.state / "trash").glob("*/sample.request.json"))
    assert remote.records("git") == []


def test_clean_unknown_needs_force(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    assert command("clean") == 1
    assert "unknown; clean requires --force" in capsys.readouterr().err


def test_keep(remote: Remote) -> None:
    assert launch(remote) == 0
    assert command("keep") == 0
    assert command("keep") == 0
    assert (remote.state / "sample.keep").read_text() == ""
    assert (remote.state / "sample.keep").stat().st_mode & 0o777 == 0o600


def finish_run_files(remote: Remote) -> None:
    for suffix in runfiles_names.SUFFIXES:
        if suffix not in {".owner", ".rc.fallback"}:
            remote.file(suffix, "finished" if suffix == ".phase" else "0")


def test_clean_moves_every_run_file_with_one_utc_stamp(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    finish_run_files(remote)
    (remote.state / "index.jsonl").write_text("preserve\n")

    class FixedTime:
        @staticmethod
        def now(tz: dt.tzinfo) -> dt.datetime:
            assert tz is dt.UTC
            return dt.datetime(2026, 11, 9, 1, 2, 3, 456789, tzinfo=dt.UTC)

    monkeypatch.setattr(cleanup, "datetime", FixedTime)
    previous = os.umask(0)
    try:
        assert command("clean", "--force") == 0
    finally:
        os.umask(previous)
    stamp = "20261109T010203456789Z"
    local = remote.local / "tbhbdev/agent-runs/trash" / stamp
    assert "trash" in {entry.name for entry in metadata.directory().iterdir()}
    assert local.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in local.iterdir()] == ["sample.json"]
    trash = remote.state / "trash" / f"{stamp}-sample"
    assert (trash / "worktree").is_dir()
    assert sorted(p.name for p in trash.iterdir() if p.is_file()) == sorted(
        "sample" + suffix
        for suffix in runfiles_names.SUFFIXES
        if suffix != ".rc.fallback"
    )
    assert all(
        p.read_text() == ("finished" if p.suffix == ".phase" else "0")
        for p in trash.iterdir()
        if p.is_file() and p.suffix != ".owner"
    )
    assert (remote.state / "index.jsonl").read_text() == "preserve\n"


def test_cleaned_run_archives_late_files(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.file(".rc", "0\n")
    with runfiles.locked(remote.state / "sample.run.lock"):
        assert command("clean", "--force") == 1
        assert (remote.state / "sample.rc").is_file()
    assert command("clean", "--force") == 0
    assert not metadata.metadata_path("sample").exists()
    assert not list(remote.state.glob("sample.*"))
    capsys.readouterr()

    late = (".audit.json", ".launched", ".log", ".review")
    for suffix in late:
        remote.file(suffix, "late\n")
    with monkeypatch.context() as host:
        host.setenv("XDG_STATE_HOME", str(remote.state.parent.parent))
        assert command("status") == 0
        assert "phase=cleaned" in capsys.readouterr().out
    assert command("clean", "--force") == 0

    output = capsys.readouterr().out
    for suffix in late:
        assert f"sample{suffix}" in output
        assert any(
            path.read_text() == "late\n"
            for path in (remote.state / "trash").glob(f"*/sample{suffix}")
        )
    assert not list(remote.state.glob("sample.*"))


@pytest.mark.parametrize("finished", [False, True])
def test_clean_without_remote_clone(remote: Remote, finished: bool) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    clone = remote.root / "home/Code/github.com/example-org/example-repo"
    clone.rename(remote.root / "saved-clone")
    if finished:
        remote.file(".rc", "0")
    assert command("clean", "--force") == 0
    assert not metadata.metadata_path("sample").exists()
    assert remote.records("git") == []


@pytest.mark.parametrize("ownerless", [False, True])
def test_clean_allows_name_reuse(remote: Remote, *, ownerless: bool) -> None:
    assert launch(remote) == 0
    remote.file(".run.lock", "")
    remote.configure(session=False)
    if ownerless:
        (remote.state / "sample.owner").unlink()
        remote.configure(branch=False)
    else:
        remote.file(".rc", "0\n")
    for suffix in (".branch", ".pr", ".head"):
        remote.file(suffix, "previous run\n")
    options = () if ownerless else ("--force",)
    assert command("clean", *options) == 0
    assert not list(remote.state.glob("sample.*"))
    assert not metadata.metadata_path("sample").exists()
    assert command("clean") == 0
    assert launch(remote) == 0


def test_clean_releases_name_after_wait_archives_run(remote: Remote) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    (remote.state / "sample.owner").unlink()
    for suffix in (".branch", ".pr", ".head"):
        remote.file(suffix, "previous run\n")

    assert command("wait") == 1
    assert not metadata.metadata_path("sample").exists()
    assert list((metadata.directory() / "trash").glob("*/sample.json"))
    for suffix in (".branch", ".pr", ".head", ".request.json"):
        assert (remote.state / f"sample{suffix}").exists()

    assert command("clean") == 0
    assert not list(remote.state.glob("sample.*"))
    assert not metadata.metadata_path("sample").exists()
    assert command("clean") == 0
    assert launch(remote) == 0


def test_clean_ownerless_run_with_a_live_session(remote: Remote) -> None:
    assert launch(remote) == 0
    (remote.state / "sample.owner").unlink()
    remote.file(".branch", "agent/sample\n")
    assert command("clean", "--force") == 1
    assert (remote.state / "sample.branch").exists()
    assert metadata.metadata_path("sample").exists()


def test_clean_fallback_requires_force(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    ticking(monkeypatch)
    assert command("kill") == 0
    assert command("clean") == 1
    assert command("clean", "--force") == 0


def test_lost_clean_reply_cannot_clean_new_owner(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.file(".rc", "0\n")
    original = transport.shell

    def lose(
        config: model.Settings,
        host: model.Host,
        script: str,
        stdin: transport.Stdin | None = None,
        *,
        timeout: float = 30,
    ) -> str:
        result = original(config, host, script, stdin, timeout=timeout)
        if 'trash="$d/trash/' in script:
            remote.file(".owner", "new-owner\n")
            remote.file(".rc", "0\n")
            message = "lost clean reply"
            raise transport.TransportError(message)
        return result

    monkeypatch.setattr(transport, "shell", lose)
    assert command("clean", "--force") == 1
    monkeypatch.setattr(transport, "shell", original)
    before = {p.name: p.read_bytes() for p in remote.state.iterdir() if p.is_file()}
    assert command("clean", "--force") == 1
    assert "not owner" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()
    assert command("clean", "--force") == 1
    assert {
        p.name: p.read_bytes() for p in remote.state.iterdir() if p.is_file()
    } == before


def test_clean_retry_after_rc_was_trashed(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.file(".rc", "0\n")
    path = metadata.metadata_path("sample")
    before = path.read_bytes()
    original = transport.shell

    def fail(
        config: model.Settings,
        host: model.Host,
        script: str,
        stdin: transport.Stdin | None = None,
        *,
        timeout: float = 30,
    ) -> str:
        if 'trash="$d/trash/' in script:
            (remote.state / "sample.rc").rename(remote.root / "saved-rc")
            message = "failed before owner move"
            raise transport.TransportError(message)
        return original(config, host, script, stdin, timeout=timeout)

    monkeypatch.setattr(transport, "shell", fail)
    assert command("clean", "--force") == 1
    assert path.read_bytes() == before
    monkeypatch.setattr(transport, "shell", original)
    assert command("clean") == 1
    assert command("clean", "--force") == 0


def test_unsandboxed_force_clean_refuses_live_session(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--account", "ops", "--unsandboxed", "o/r#1") == 0
    before = remote.records("git")
    assert command("clean", "--force") == 1
    assert "run is running; clean refused" in capsys.readouterr().err
    assert metadata.metadata_path("sample").exists()
    assert (remote.state / "sample.owner").is_file()
    assert not (remote.state / "trash").exists()
    assert remote.records("git") == before


def _local_run(home: Path, *, worktree: bool) -> tuple[runfiles.RunFiles, Path]:
    files = runfiles.RunFiles(metadata.directory(), "sample")
    files.write("request.json", json.dumps(request.to_dict(BUILD)))
    files.write("rc", "0\n")
    clone = home / "Code" / "github.com" / "example-org" / "example-repo"
    tree = clone / ".worktrees" / "agent-sample"
    if worktree:
        tree.mkdir(parents=True)
        (tree / "work.txt").write_text("kept\n")
    return files, tree


@pytest.mark.usefixtures("remote")
def test_local_clean_moves_the_worktree_into_the_trash(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files, tree = _local_run(isolated_home, worktree=True)
    assert command("clean", "--force") == 0
    assert "moved worktree" in capsys.readouterr().out
    assert not tree.exists()
    (entry,) = (files.root / "trash").glob("*-sample")
    assert (entry / "worktree" / "work.txt").read_text() == "kept\n"
    assert (entry / "sample.request.json").is_file()


@pytest.mark.usefixtures("remote")
def test_local_clean_without_a_worktree(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files, _ = _local_run(isolated_home, worktree=False)
    assert command("clean", "--force") == 0
    assert "moved worktree" not in capsys.readouterr().out
    (entry,) = (files.root / "trash").glob("*-sample")
    assert not (entry / "worktree").exists()


@pytest.mark.usefixtures("remote")
@pytest.mark.parametrize(
    ("symlink", "message"),
    [(True, "is a symlink"), (False, "is not a directory")],
)
def test_local_clean_refuses_a_misplaced_worktree(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    symlink: bool,
    message: str,
) -> None:
    files, tree = _local_run(isolated_home, worktree=False)
    tree.parent.mkdir(parents=True)
    if symlink:
        tree.symlink_to(isolated_home)
    else:
        tree.write_text("not a clone\n")
    assert command("clean", "--force") == 1
    assert message in capsys.readouterr().err
    assert tree.exists(follow_symlinks=False)
    assert files.path("request.json").is_file()
    assert not (files.root / "trash").exists()
