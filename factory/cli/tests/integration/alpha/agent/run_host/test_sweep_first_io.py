"""The run host sweeps the machine once before it admits a run."""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING
lazy from pathlib import Path

import pytest
from agent_host_example import make_run
from run_host_fixtures import HOST_FIXTURES

from tbhbdev.alpha.agent import admission, host_tools, process_group
from tbhbdev.alpha.agent.run_host import execution, sweep_first

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.run_host.state import Run

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES
REAL_SWEEP = sweep_first.run_sweep


class FakeChild:
    pid = 4242

    def __init__(self, *, rc: int = 0, hangs: bool = False) -> None:
        self.rc, self.hangs = rc, hangs
        self.returncode: int | None = None
        self.timeouts: list[float] = []

    def wait(self, timeout: float | None = None) -> int:
        self.timeouts.append(timeout or 0)
        if self.hangs and self.returncode is None:
            message = "sweep"
            raise subprocess.TimeoutExpired(message, timeout or 0)
        self.returncode = self.rc
        return self.rc

    def poll(self) -> int | None:
        return self.returncode


class Harness:
    """Fake the sweep child and record the order of launch steps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **child: object) -> None:
        self.events: list[str] = []
        self.commands: list[tuple[str, ...]] = []
        self.stops: list[tuple[int, float]] = []
        self.child = FakeChild(**child)  # type: ignore[arg-type]
        self.start_error: OSError | None = None
        monkeypatch.setattr(sweep_first, "run_sweep", REAL_SWEEP)
        monkeypatch.setattr(process_group, "launch", self.launch)
        monkeypatch.setattr(process_group, "stop_started", self.stop)
        monkeypatch.setattr(execution, "_execute", self.body)
        real = admission.reserve

        @contextmanager
        def reserve(*args: object) -> Generator[None]:
            self.events.append("reserve")
            with real(*args):  # type: ignore[arg-type]
                yield

        monkeypatch.setattr(admission, "reserve", reserve)

    def launch(self, command: tuple[str, ...], **_options: object) -> FakeChild:
        self.events.append("sweep")
        self.commands.append(command)
        if self.start_error is not None:
            raise self.start_error
        return self.child

    def stop(self, pgid: int, grace: float, _reap: object = None) -> bool:
        self.stops.append((pgid, grace))
        self.child.returncode = -9
        return True

    def body(self, *_args: object, **_options: object) -> int:
        self.events.append("body")
        return 0


def log(run: Run) -> str:
    path = run.files.path("log")
    return path.read_text() if path.exists() else ""


def test_the_command_runs_this_build_and_never_searches_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_options: object) -> str:
        pytest.fail("the sweep program was looked up by name")

    monkeypatch.setattr(shutil, "which", refuse)
    monkeypatch.setattr(host_tools, "resolve", refuse)
    monkeypatch.setenv("PATH", "")
    harness = Harness(monkeypatch)
    execution.execute(make_run(tmp_path), "brief")
    assert harness.commands == [
        (sys.executable, "-I", "-m", "tbhbdev", "alpha", "agent", "sweep")
    ]


def test_the_sweep_runs_once_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch)
    run = make_run(tmp_path)
    assert execution.execute(run, "brief") == 0
    assert harness.events == ["sweep", "reserve", "body"]
    assert harness.child.timeouts == [sweep_first.SWEEP_LIMIT_SECONDS]
    assert log(run) == ""


@pytest.mark.parametrize("rc", [1, 2, 137])
def test_a_failing_sweep_is_logged_and_the_launch_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    harness = Harness(monkeypatch, rc=rc)
    run = make_run(tmp_path)
    assert execution.execute(run, "brief") == 0
    assert harness.events == ["sweep", "reserve", "body"]
    assert log(run) == f"sweep: exited {rc}; the launch goes on\n"


def test_a_sweep_that_cannot_start_is_logged_and_the_launch_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch)
    harness.start_error = FileNotFoundError("no interpreter")
    run = make_run(tmp_path)
    assert execution.execute(run, "brief") == 0
    assert harness.events == ["sweep", "reserve", "body"]
    line = "sweep: could not start (FileNotFoundError); the launch goes on\n"
    assert log(run) == line


def test_a_hanging_sweep_is_stopped_by_group_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch, hangs=True)
    run = make_run(tmp_path)
    assert execution.execute(run, "brief") == 0
    assert harness.events == ["sweep", "reserve", "body"]
    assert harness.child.timeouts == [sweep_first.SWEEP_LIMIT_SECONDS]
    assert harness.stops == [(FakeChild.pid, run.settings.kill_grace_seconds)]
    assert log(run) == "sweep: stopped after 60s; the launch goes on\n"


def test_a_finished_sweep_is_not_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch)
    execution.execute(make_run(tmp_path), "brief")
    assert harness.stops == []


def test_preflight_does_not_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch)
    execution.execute(make_run(tmp_path), "brief", preflight_only=True)
    assert harness.events == ["body"]


def test_a_refused_launch_does_not_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path, max_concurrent_runs=1)
    with admission.reserve(admission.ROOT, run.host, run.request.profile, 3):
        harness = Harness(monkeypatch)
        with pytest.raises(admission.SlotFullError):
            execution.execute(run, "brief")
    assert harness.events == ["reserve"]
    assert log(run) == ""
