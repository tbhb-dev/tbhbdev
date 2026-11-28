"""The run host sweeps the machine once before it admits a run."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
lazy from pathlib import Path

import pytest
from agent_host_example import make_run
from run_host_fixtures import HOST_FIXTURES

from tbhbdev.alpha.agent import admission, process_group, sweep
from tbhbdev.alpha.agent.run_host import execution, sweep_first
from tbhbdev.alpha.agent.run_host.state import RunInterruptedError

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.run_host.state import Run

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES
REAL_SWEEP = sweep_first.run_sweep


class Harness:
    """Record the order of the sweep and launch steps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.rc = 0
        self.error: Exception | None = None
        monkeypatch.setattr(sweep_first, "run_sweep", REAL_SWEEP)
        monkeypatch.setattr(sweep, "main", self.sweep)
        monkeypatch.setattr(process_group, "launch", self.launch)
        monkeypatch.setattr(execution, "_execute", self.body)
        real = admission.reserve

        @contextmanager
        def reserve(*args: object) -> Generator[None]:
            self.events.append("reserve")
            with real(*args):  # type: ignore[arg-type]
                yield

        monkeypatch.setattr(admission, "reserve", reserve)

    def sweep(self, args: list[str], _prog: str) -> int:
        self.events.append("sweep")
        assert args == []
        if self.error is not None:
            raise self.error
        return self.rc

    def launch(self, *_args: object, **_options: object) -> None:
        pytest.fail("the launch started a separate sweep program")

    def body(self, *_args: object, **_options: object) -> int:
        self.events.append("body")
        return 0


def log(run: Run) -> str:
    path = run.files.path("log")
    return path.read_text() if path.exists() else ""


def test_the_sweep_runs_once_in_process_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch)
    run = make_run(tmp_path)
    assert execution.execute(run, "brief") == 0
    assert harness.events == ["sweep", "reserve", "body"]
    assert log(run) == ""


@pytest.mark.parametrize("rc", [1, 2, 137])
def test_a_failing_sweep_is_logged_and_the_launch_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    harness = Harness(monkeypatch)
    harness.rc = rc
    run = make_run(tmp_path)
    assert execution.execute(run, "brief") == 0
    assert harness.events == ["sweep", "reserve", "body"]
    assert log(run) == f"sweep: exited {rc}; the launch goes on\n"


def test_a_sweep_that_raises_is_logged_and_the_launch_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch)
    harness.error = RuntimeError("sweep failed")
    run = make_run(tmp_path)
    assert execution.execute(run, "brief") == 0
    assert harness.events == ["sweep", "reserve", "body"]
    assert log(run) == "sweep: failed (RuntimeError); the launch goes on\n"


def test_run_interruption_during_sweep_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch)
    harness.error = RunInterruptedError()
    run = make_run(tmp_path)
    with pytest.raises(RunInterruptedError):
        execution.execute(run, "brief")
    assert harness.events == ["sweep"]
    assert log(run) == ""


def test_preflight_does_not_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch)
    execution.execute(make_run(tmp_path), "brief", preflight_only=True)
    assert harness.events == ["body"]


def test_a_slot_full_launch_sweeps_once_and_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path, max_concurrent_runs=1)
    with admission.reserve(admission.ROOT, run.host, run.request.profile, 3):
        harness = Harness(monkeypatch)
        with pytest.raises(admission.SlotFullError):
            execution.execute(run, "brief")
    assert harness.events == ["sweep", "reserve"]
    assert log(run) == ""
