"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
lazy import subprocess
lazy from collections.abc import Callable

import pytest
from agent_host_example import (
    constant,
    make_run,
)
from run_host_fixtures import (
    Child,
    preparation,
)

from tbhbdev.alpha.agent import (
    credentials,
    process_group,
    push,
    runfiles,
    worktree,
)
from tbhbdev.alpha.agent.run_host import auth as host_auth
from tbhbdev.alpha.agent.run_host import completion as host_completion
from tbhbdev.alpha.agent.run_host import execution as host_execution
from tbhbdev.alpha.agent.run_host import harness as host_harness
from tbhbdev.alpha.agent.run_host import preflight as host_preflight
from tbhbdev.alpha.agent.run_host import workspace as host_workspace

pytestmark = pytest.mark.usefixtures(
    "empty_bazel_listing", "fake_secrets", "portable_run_protocol", "stub_push"
)


def test_agent_cleanup_stops_and_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Child()
    stopped: list[int] = []

    def stop(pid: int, grace: float, reap: Callable[[], object] | None = None) -> bool:
        assert grace == 0
        assert reap == child.poll
        stopped.append(pid)
        return True

    monkeypatch.setattr(process_group, "stop_started", stop)
    host_auth.stop_agent(cast("subprocess.Popen[str]", child))
    assert stopped == [23]
    assert child.waits == [None]


def test_auth_skip_reason_is_logged_with_failure_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(host_harness, "run_harness", constant(0))
    monkeypatch.setattr(
        credentials, "auth_write_back", constant("unsafe auth skipped\n")
    )
    runfiles.atomic(Path(prepared.paths.codex_home) / "auth.json", "{}")
    assert host_execution.execute(run, "example") == 1
    assert "unsafe auth skipped" in run.files.path("log").read_text()


def test_refused_auth_is_moved_whole_to_private_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    source = runfiles.private_dir(run.home / ".codex") / "auth.json"
    runfiles.atomic(source, '{"tokens":{}}')
    prepared = dataclasses.replace(prepared, auth_before=source.read_text())
    target = Path(prepared.paths.codex_home) / "auth.json"
    refreshed = '{"tokens":{"access_token":"fixture-live-token"}}'
    original_datetime = datetime

    class FrozenClock:
        @staticmethod
        def now(zone: object) -> datetime:
            assert zone is UTC
            return original_datetime(2026, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(host_auth, "datetime", FrozenClock)
    mkdir = Path.mkdir
    names = iter(
        (
            "example-20260101T000000.000000Z",
            "example-20260101T000000.000000Z",
            "example-20260101T000000.000001Z",
        )
    )

    def checked_mkdir(
        path: Path, mode: int = 0o777, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path.parent == run.home / ".codex-recovery":
            assert path.name == next(names)
            assert mode == 0o700
            assert parents is False
            assert exist_ok is False
        mkdir(path, mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", checked_mkdir)
    expected_log = ""
    for _ in range(2):
        runfiles.private_dir(target.parent)
        runfiles.atomic(target, refreshed)
        inode = target.stat().st_ino
        assert host_completion.finish(run, prepared, None, 0) == 1
        recovered = next(
            path
            for path in (run.home / ".codex-recovery").glob("*/auth.json")
            if path.stat().st_ino == inode
        )
        assert recovered.read_text() == refreshed
        assert stat.S_IMODE(recovered.stat().st_mode) == 0o600
        assert stat.S_IMODE(recovered.parent.stat().st_mode) == 0o700
        expected_log += (
            "codex auth write-back skipped: tokens.id_token: found missing; "
            f"expected string; recovery: {recovered}\n"
        )
        assert run.files.path("log").read_text() == expected_log
    assert len(list((run.home / ".codex-recovery").glob("*/auth.json"))) == 2
    assert {p.name for p in (run.home / ".codex-recovery").iterdir()} == {
        "example-20260101T000000.000000Z",
        "example-20260101T000000.000001Z",
    }
    assert source.read_text() == prepared.auth_before
    assert "fixture-live-token" not in run.files.path("log").read_text()


def test_unproven_harness_stop_keeps_rc_and_withholds_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(
        host_preflight,
        "resolve_harness",
        constant(
            (host_preflight.resolve_harness(run, prepared.paths)[0], "/example/harness")
        ),
    )
    child = Child()

    def launch(
        _spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        started(child)
        return child

    def denied(_pgid: int, _grace: float, *_args: object) -> None:
        raise PermissionError

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", denied)
    assert host_execution.execute(run, "example") == 7
    assert run.files.path("phase").read_text() == "unknown"
    assert run.files.path("rc").read_text() == "7\n"
    log = run.files.path("log").read_text().splitlines()
    assert log[0] == "unsandboxed full access authorized_by=None"
    assert log[1].startswith("argv: ")
    assert log[2:] == ["harness exited 7", "run group stop unproven; phase unknown"]
    assert not (run.home / ".codex" / "auth.json").exists()


@pytest.mark.parametrize("failure", [False, True])
def test_finish_removes_private_state_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    run = make_run(tmp_path)
    workspace = runfiles.private_dir(run.home / "worktree")
    monkeypatch.setattr(worktree, "create", constant(workspace))
    monkeypatch.setattr(push, "head", constant("a" * 40))
    prepared = host_workspace.prepare_workspace(run)
    directory = Path(prepared.paths.codex_home)
    assert directory == tmp_path / "state" / "tbhbdev" / "agent-codex" / run.files.name
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def write_back(_source: Path, destination: Path, _before: str | None) -> None:
        assert destination.parent == directory
        assert directory.is_dir()
        if failure:
            raise OSError

    monkeypatch.setattr(credentials, "auth_write_back", write_back)
    assert host_completion.finish(run, prepared, None, 0) == int(failure)
    assert directory.exists() is failure
