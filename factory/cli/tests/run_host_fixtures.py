"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import getpass
import hashlib
import os
import subprocess
from contextlib import ExitStack
from pathlib import Path
lazy from collections.abc import Callable

import pytest
from agent_host_example import make_run

from tbhbdev.alpha.agent import (
    credentials,
    host_tools,
    process_group,
    push,
    runfiles,
)
from tbhbdev.alpha.agent.model import (
    Invalid,
    RunPaths,
)
from tbhbdev.alpha.agent.run_host import auth as host_auth
from tbhbdev.alpha.agent.run_host import bazel_cache as host_bazel_cache
from tbhbdev.alpha.agent.run_host import bazel_shutdown as host_bazel_shutdown
from tbhbdev.alpha.agent.run_host import harness as host_harness
from tbhbdev.alpha.agent.run_host import state

HOST_FIXTURES = pytest.mark.usefixtures(
    "empty_bazel_listing", "fake_secrets", "portable_run_protocol", "stub_push"
)


def preparation(run: state.Run) -> state.Prepared:
    paths = RunPaths(
        str(run.home),
        str(runfiles.private_dir(run.home / "worktree")),
        str(run.tmpdir),
        str(runfiles.private_dir(run.tmpdir.parent / "codex")),
        str(run.tmpdir / "audit"),
    )
    return state.Prepared(
        paths,
        credentials.Delivery({}, None, claude_profile=None),
        None,
    )


class Child:
    pid = 23

    def __init__(self, code: int = 7, *, timeout: bool = False) -> None:
        self.code = code
        self.timeout = timeout
        self.waits: list[int | None] = []

    def poll(self) -> int:
        return self.code

    def wait(self, timeout: int | None = None) -> int:
        self.waits.append(timeout)
        if self.timeout and len(self.waits) == 1:
            assert timeout is not None
            command = "example-harness"
            raise subprocess.TimeoutExpired(command, timeout)
        return self.code


def bazel_run(tmp_path: Path) -> tuple[state.Run, state.Prepared]:
    run = make_run(tmp_path)
    prepared = preparation(run)
    bases = host_bazel_cache.bazel_bases(run.home, prepared.paths.worktree)
    return run, dataclasses.replace(prepared, bases=bases)


def make_base(prepared: state.Prepared) -> Path:
    base = prepared.bases[0]
    base.mkdir(parents=True)
    return base


def stop_and_clean(run: state.Run, prepared: state.Prepared) -> dict[str, int]:
    return host_bazel_cache.cleanup_bazel(
        run, prepared, host_bazel_shutdown.shutdown_bazel(run, prepared)
    )


def bazel_base(run: state.Run, worktree: Path) -> Path:
    name = hashlib.md5(
        os.path.realpath(worktree).encode(), usedforsecurity=False
    ).hexdigest()
    return run.home / ".cache/bazel" / f"_bazel_{getpass.getuser()}" / name


class HostRuntime:
    def __init__(
        self, run: state.Run, prepared: state.Prepared, failure: str | None
    ) -> None:
        self.run, self.prepared, self.failure = run, prepared, failure
        self.events: list[str] = []
        self.phases: list[str] = []
        self.agent = Child()
        self.write_file = run.files.write

    def prepare(
        self,
        operation: Callable[[], object],
        files: runfiles.RunFiles,
        deadline: float,
        grace: float,
    ) -> state.Prepared:
        assert callable(operation)
        assert files is self.run.files
        assert deadline == self.run.deadline
        assert grace == self.run.settings.kill_grace_seconds
        if self.failure == "timeout":
            raise TimeoutError
        if self.failure == "interrupt":
            raise state.RunInterruptedError
        if self.failure == "invalid":
            message = "safe preparation error"
            raise Invalid(message)
        if self.failure == "os":
            message = "unprinted-error-material"
            raise OSError(message)
        return self.prepared

    def harness(
        self, run: state.Run, prepared: state.Prepared, stack: ExitStack
    ) -> int:
        assert run is self.run
        assert prepared is self.prepared
        assert isinstance(stack, ExitStack)
        self.events.append("group-stopped")
        return 7

    def sign(self, key: str, tmpdir: Path, deadline: float) -> Child:
        assert key == "example-key"
        assert tmpdir == self.run.tmpdir
        assert deadline == self.run.deadline
        self.events.append("agent-started")
        return self.agent

    def write_back(self, source: Path, destination: Path, before: str | None) -> None:
        assert source == self.run.home / ".codex" / "auth.json"
        assert destination == Path(self.prepared.paths.codex_home) / "auth.json"
        assert before == "example-before"
        self.events.append("auth")
        if self.failure == "auth":
            raise OSError

    def stop(self, agent: Child) -> None:
        assert agent is self.agent
        self.events.append("agent-stopped")

    def write(self, suffix: str, text: str) -> None:
        if suffix == "phase":
            self.phases.append(text)
        if suffix == "rc":
            assert self.phases[-1] == "ended"
            entry = runfiles.latest(self.run.files.root).get("example")
            assert entry is None or entry["rc"] is None
        self.write_file(suffix, text)

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_group, "prepare", self.prepare)
        monkeypatch.setattr(host_harness, "run_harness", self.harness)
        monkeypatch.setattr(credentials, "signing_agent", self.sign)
        monkeypatch.setattr(credentials, "auth_write_back", self.write_back)
        monkeypatch.setattr(host_auth, "stop_agent", self.stop)
        monkeypatch.setattr(self.run.files, "write", self.write)


def harness_without_push(
    run: state.Run,
    prepared: state.Prepared,
    stack: ExitStack,
    harness: Callable[[state.Run, state.Prepared, ExitStack], int],
) -> int:
    return harness(run, prepared, stack)


RESOLVE = host_tools.resolve


PUSH_RUN = push.run
