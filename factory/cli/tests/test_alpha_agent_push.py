"""Record local build history when the harness runs."""

from __future__ import annotations

import os
import subprocess
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_host_example import host_settings, run_request
from runfile_fixtures import agent_suffix_case as agent_suffix_case  # noqa: PLC0414

from tbhbdev.alpha.agent import (
    credentials,
    push,
    run_host,
    runfiles,
)

if TYPE_CHECKING:
    from collections.abc import Generator

type LocalRun = tuple[run_host.Run, run_host.Prepared, Path]


@pytest.fixture(autouse=True)
def umask_022() -> Generator[None]:
    """Create the private-name list as a 0644 file whatever the host umask is."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


@pytest.fixture(autouse=True)
def push_account(
    isolated_agent_home: None, agent_suffix_case: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del isolated_agent_home, agent_suffix_case
    original_read = runfiles.read_owned

    def read(source: Path, limit: int, label: str) -> str:
        for part in source.parts:
            if part.lower() == ".git":
                assert part == ".git"
        if source.name.lower() in ("head", "packed-refs"):
            assert source.name in ("HEAD", "packed-refs")
        expected = {
            "HEAD": (1024, "run HEAD"),
            "packed-refs": (1024 * 1024, "packed refs"),
        }
        assert (limit, label) == expected.get(source.name, (1024, "run ref"))
        return original_read(source, limit, label)

    monkeypatch.setattr(runfiles, "read_owned", read)


@pytest.fixture
def local_run(tmp_path: Path) -> LocalRun:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target.git"
    git(workspace, "init", "--initial-branch=agent/example")
    git(workspace, "init", "--bare", str(target))
    git(workspace, "config", "user.name", "Example")
    git(workspace, "config", "user.email", "example@example.com")
    git(workspace, "config", "commit.gpgsign", "false")
    commit(workspace, "initial")
    paths = run_host.RunPaths(
        str(tmp_path),
        str(workspace),
        str(tmp_path / "tmp"),
        str(tmp_path / "codex"),
        str(tmp_path / "audit"),
    )
    settings = host_settings()
    run = run_host.Run(
        runfiles.RunFiles(tmp_path / "state", "example"),
        run_request(),
        settings,
        settings.hosts[0],
        tmp_path,
        tmp_path / "tmp",
        0,
        100,
    )
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )

    return run, prepared, target


def git(directory: Path, *args: str, data: bytes | None = None) -> str:
    return (
        subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
            cwd=directory,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(Path.home()),
                "XDG_CONFIG_HOME": str(Path.home() / ".config"),
                "XDG_STATE_HOME": str(Path.home() / ".local/state"),
                "XDG_CACHE_HOME": str(Path.home() / ".cache"),
                "XDG_DATA_HOME": str(Path.home() / ".local/share"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
            input=data,
        )
        .decode()
        .strip()
    )


def commit(directory: Path, text: str) -> None:
    git(directory, "commit", "--allow-empty", "-qm", text)


def harness(run: run_host.Run, prepared: run_host.Prepared, stack: ExitStack) -> int:
    assert isinstance(stack, ExitStack)
    commit(Path(prepared.paths.worktree), "agent change")
    run.files.write("out.final", "report")
    return 0


START_REF = "refs/heads/fix/topic"


@pytest.mark.parametrize("start_ref", [START_REF, None])
@pytest.mark.parametrize("pushed_in_run", [False, True])
def test_build_records_its_commit_and_verifies_the_harness_push(
    local_run: LocalRun,
    monkeypatch: pytest.MonkeyPatch,
    start_ref: str | None,
    *,
    pushed_in_run: bool,
) -> None:
    run, prepared, target = local_run
    run.request = replace(run.request, expect_commit=True, start_ref=start_ref)
    workspace = Path(prepared.paths.worktree)
    branch = start_ref or "refs/heads/agent/example"
    if start_ref is not None:
        git(workspace, "push", target.as_uri(), f"HEAD:{branch}")
    base = git(workspace, "rev-parse", "HEAD")

    def work(actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack) -> int:
        rc = harness(actual, paths, stack)
        if pushed_in_run:
            git(workspace, "push", target.as_uri(), f"HEAD:{branch}")
        return rc

    def origin_contains(_run: run_host.Run, head: str) -> bool:
        tip = git(target, "for-each-ref", "--format=%(objectname)", branch)
        return bool(tip) and head in git(target, "rev-list", tip).splitlines()

    monkeypatch.setattr(run_host, "run_harness", work)
    monkeypatch.setattr(run_host, "origin_contains", origin_contains)
    with ExitStack() as stack:
        assert run_host.run_and_check(run, prepared, stack) == (
            0 if pushed_in_run else run_host.NO_PUSH
        )
    sha = git(workspace, "rev-parse", "HEAD")
    expected = f"{sha} {branch}" if pushed_in_run else ""
    if not pushed_in_run and start_ref is not None:
        expected = f"{base} {branch}"
    assert git(target, "for-each-ref", "--format=%(objectname) %(refname)") == expected
    assert_run_file(run, "head", sha)
    assert_run_file(
        run, "branch", branch.removeprefix("refs/heads/") if pushed_in_run else None
    )
    assert not run.files.path("pr").exists()


def test_do_not_record_uncertain_run(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    run.files.uncertain = True
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness) == 0
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("log").exists()


def assert_run_file(run: run_host.Run, name: str, expected: str | None) -> None:
    path = run.files.path(name)
    assert (path.read_text() if path.exists() else None) == expected


@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_head_reads_only_owned_ref_data(
    packed: bool, valid: bool, local_run: tuple[run_host.Run, run_host.Prepared, Path]
) -> None:
    _run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    expected = git(workspace, "rev-parse", "HEAD")
    if packed:
        git(workspace, "pack-refs", "--all")
        if not valid:
            (workspace / ".git" / "packed-refs").write_text(
                "# pack-refs\n" + expected + " refs/heads/other\n"
            )
    elif not valid:
        (workspace / ".git" / "refs/heads/agent/example").write_text("bad\n")
    if valid:
        assert push.head(workspace, "agent/example") == expected
    else:
        message = "missing run commit" if packed else "invalid run commit"
        with pytest.raises(ValueError, match=rf"^{message}$"):
            push.head(workspace, "agent/example")


@pytest.mark.parametrize("padding", [" ", "\t"])
def test_packed_ref_rejects_leading_whitespace(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], padding: str
) -> None:
    _run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    expected = git(workspace, "rev-parse", "HEAD")
    git(workspace, "pack-refs", "--all")
    (workspace / ".git" / "packed-refs").write_text(
        f"{padding}{expected} refs/heads/agent/example\n"
    )
    with pytest.raises(ValueError, match=r"^invalid run commit$"):
        push.head(workspace, "agent/example")


@pytest.mark.parametrize("start_ref", [START_REF, None])
@pytest.mark.parametrize("on_run_branch", [True, False])
def test_timed_out_build_keeps_local_branch(
    local_run: LocalRun,
    start_ref: str | None,
    on_run_branch: bool,
) -> None:
    run, prepared, target = local_run
    run.request = replace(run.request, start_ref=start_ref)
    workspace = Path(prepared.paths.worktree)
    base = git(workspace, "rev-parse", "HEAD")

    def failed_harness(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        if not on_run_branch:
            git(Path(paths.paths.worktree), "switch", "-c", "topic/work")
        harness(actual, paths, stack)
        return 124

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, failed_harness) == 124
    tip = git(workspace, "rev-parse", "HEAD")
    branch = "agent/example" if on_run_branch else "topic/work"
    assert git(workspace, "rev-parse", branch) == tip
    assert git(target, "for-each-ref") == ""
    assert_run_file(run, "base", base)
    assert_run_file(run, "head", tip)
    expected = f"start ref: {start_ref} at {base}\n" if start_ref else None
    assert_run_file(run, "log", expected)


@pytest.mark.parametrize("missing", [False, True])
def test_preserves_harness_failure_when_git_metadata_is_unusable(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], missing: bool
) -> None:
    run, prepared, target = local_run

    def failed(_run: run_host.Run, paths: run_host.Prepared, _stack: ExitStack) -> int:
        directory = Path(paths.paths.worktree) / ".git"
        if missing:
            (directory / "HEAD").unlink()
        else:
            (directory / "refs" / "heads" / "agent" / "example").write_text("invalid\n")
        return 7

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, failed) == 7
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("log").exists()
