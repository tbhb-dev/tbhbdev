"""Run-host sweep ordering and failure handling with a fake child process."""

from __future__ import annotations

import dataclasses
import signal
import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

import pytest
from agent_host_example import make_run

from tbhbdev.alpha.agent import admission, process_group
from tbhbdev.alpha.agent.run_host import execution

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from tbhbdev.alpha.agent.model import Host, Profile
    from tbhbdev.alpha.agent.run_host.state import Run


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Run, Mock, Mock]:
    run = make_run(tmp_path, max_concurrent_runs=1)
    start = Mock(return_value=Mock(pid=123, **{"wait.return_value": 0}))
    execute = Mock(return_value=7)
    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(execution, "_execute", execute)
    monkeypatch.setattr(admission, "ROOT", tmp_path / "admission")
    return run, start, execute


def test_sweep_runs_once_between_slot_check_and_startup(
    launch: tuple[Run, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    run, start, execute = launch
    events = Mock()
    events.attach_mock(start, "start_sweep")
    events.attach_mock(start.return_value.wait, "wait_for_sweep")
    events.attach_mock(execute, "execute")
    reserve = admission.reserve

    @contextmanager
    def record_reserve(
        root: Path, host: Host, profile: Profile, limit: int | None
    ) -> Generator[None]:
        with reserve(root, host, profile, limit):
            events.slot_reserved()
            yield

    monkeypatch.setattr(admission, "reserve", record_reserve)
    assert execution.execute(run, "example") == execute.return_value
    assert [event[0] for event in events.mock_calls] == [
        "slot_reserved",
        "start_sweep",
        "wait_for_sweep",
        "execute",
    ]
    start.assert_called_once()
    command = start.call_args.args[0]
    assert command == (str(run.home / ".local/bin/tbhbdev"), "alpha", "agent", "sweep")
    assert start.call_args.kwargs["start_new_session"] is True
    assert not run.files.path("log").exists()


@pytest.mark.parametrize(
    ("cannot_start", "line"),
    [(False, "agent sweep failed (exit 1)"), (True, "agent sweep could not run")],
)
def test_sweep_failure_is_logged_and_startup_continues(
    launch: tuple[Run, Mock, Mock], *, cannot_start: bool, line: str
) -> None:
    run, start, execute = launch
    if cannot_start:
        start.side_effect = FileNotFoundError
    else:
        start.return_value.wait.return_value = 1
    run.files.write("log", "existing run log\n")
    assert execution.execute(run, "example") == execute.return_value
    execute.assert_called_once()
    assert run.files.path("log").read_text() == f"existing run log\n{line}\n"


def test_sweep_timeout_stops_the_group_and_startup_continues(
    launch: tuple[Run, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    run, start, execute = launch
    run.settings = dataclasses.replace(run.settings, kill_grace_seconds=3)
    child = start.return_value
    child.wait.side_effect = subprocess.TimeoutExpired("tbhbdev", 60)
    send = Mock()
    wait_stopped = Mock(side_effect=[False, True])
    monkeypatch.setattr(process_group.os, "killpg", send)
    monkeypatch.setattr(process_group, "_wait_stopped", wait_stopped)
    monkeypatch.setattr(process_group.time, "monotonic", lambda: 1000)
    assert execution.execute(run, "example") == execute.return_value
    child.wait.assert_called_once_with(timeout=60)
    assert send.call_args_list == [
        call(child.pid, signal.SIGTERM),
        call(child.pid, signal.SIGKILL),
    ]
    assert wait_stopped.call_args_list == [
        call(child.pid, 1003, child.poll),
        call(child.pid, 1000.2, child.poll),
    ]
    execute.assert_called_once()
    assert (
        run.files.path("log").read_text() == "agent sweep timed out after 60 seconds\n"
    )


def test_sweep_log_write_failure_does_not_stop_startup(
    launch: tuple[Run, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    run, start, execute = launch
    start.return_value.wait.return_value = 1
    write = Mock(side_effect=OSError)
    monkeypatch.setattr(run.files, "write", write)
    assert execution.execute(run, "example") == execute.return_value
    write.assert_called_once_with("log", "agent sweep failed (exit 1)\n")


def test_full_host_refuses_without_sweeping(launch: tuple[Run, Mock, Mock]) -> None:
    run, start, execute = launch
    with (
        admission.reserve(admission.ROOT, run.host, run.request.profile, 1),
        pytest.raises(admission.SlotFullError),
    ):
        execution.execute(run, "example")
    start.assert_not_called()
    execute.assert_not_called()


def test_preflight_only_does_not_sweep(launch: tuple[Run, Mock, Mock]) -> None:
    run, start, execute = launch
    assert (
        execution.execute(run, "example", preflight_only=True) == execute.return_value
    )
    start.assert_not_called()
    execute.assert_called_once()
