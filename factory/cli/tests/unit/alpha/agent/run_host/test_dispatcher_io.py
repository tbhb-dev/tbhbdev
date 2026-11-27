"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
lazy from collections.abc import Generator

import pytest
from agent_host_example import (
    build_provenance,
    constant,
    make_run,
    run_request,
)
from run_host_fixtures import HOST_FIXTURES
from test_alpha_agent_settings import EXAMPLE

from tbhbdev.alpha.agent import (
    build_info,
    host_tools,
    process_group,
    runfiles,
)
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.decisions.run_state import Group
from tbhbdev.alpha.agent.model import (
    Invalid,
)
from tbhbdev.alpha.agent.ownership import RunExistsError
from tbhbdev.alpha.agent.run_host import dispatcher as host_dispatcher
from tbhbdev.alpha.agent.run_host import execution as host_execution
from tbhbdev.alpha.agent.run_host import intake as host_intake
from tbhbdev.alpha.agent.run_host import state

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


def test_run_host_refuses_when_host_has_zero_slots(config_file: Path) -> None:
    config_file.write_text(EXAMPLE)
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev" / "agent-runs", "example"
    )
    files.write(
        "request.json",
        json.dumps(request.to_dict(run_request(max_concurrent_runs=0))),
    )
    files.write("prompt", "example")
    assert host_dispatcher.main(["example", "--host", "example-linux"], "run-host") == 1
    assert files.path("rc").read_text() == "1\n"
    assert "max_concurrent_runs=0" in files.path("log").read_text()


@pytest.mark.parametrize(
    "case", ["host", "list", "duplicate", "rc", "pgid", "account", "profile", "missing"]
)
def test_command_refuses_invalid_input(
    tmp_path: Path,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    config_file.write_text(EXAMPLE)
    files = runfiles.RunFiles(
        Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev" / "agent-runs", "example"
    )
    files.write("prompt", "example")
    data = request.to_dict(run_request())
    if case == "account":
        data["account"] = "other"
    if case == "profile":
        data.update(harness="claude", claude_profile="unknown", reason="example")
    path = tmp_path / "received.json"
    path.write_text("[]" if case == "list" else json.dumps(data))
    if case == "duplicate":
        files.write("request.json", json.dumps(data))
    if case in ("rc", "pgid"):
        files.write(case, "0")
    arguments = [
        "example",
        str(path),
        "--host",
        "unknown" if case == "host" else "example-linux",
    ]
    if case == "missing":
        arguments = ["example", "--host", "example-linux"]
    assert host_dispatcher.main(arguments, "example run-host") == 1
    missing_path = files.path("request.json")
    details = {
        "host": "host 'unknown' is not configured",
        "list": "request must be a JSON object",
        "duplicate": "run 'example' already exists",
        "rc": "run 'example' has already started",
        "pgid": "run 'example' has already started",
        "account": "account 'other' is not enabled on example-linux",
        "profile": "claude profile 'unknown' is not configured",
        "missing": f"[Errno 2] No such file or directory: '{missing_path}'",
    }
    assert capsys.readouterr().err == f"example run-host: {details[case]}\n"
    if case in ("duplicate", "rc", "pgid"):
        assert not files.path("log").exists()
        assert (
            files.path("rc").read_text() == "0"
            if case == "rc"
            else not files.path("rc").exists()
        )
    else:
        assert files.path("rc").read_text() == "1\n"
        assert files.path("log").read_text() == f"run refused: {details[case]}\n"


@pytest.mark.parametrize("escalate", [False, True])
def test_hangup_stops_harness_group_writes_auth_and_rc(
    tmp_path: Path, escalate: bool
) -> None:
    script = r'''
import dataclasses, os, signal, subprocess, sys
from pathlib import Path
from agent_host_example import make_run
from run_host_fixtures import harness_without_push, preparation
from tbhbdev.alpha.agent import admission, host_tools, process_group, push, runfiles
from tbhbdev.alpha.agent.run_host import dispatcher, intake
binary = str(Path(sys.argv[2]) / "host-bin" / "true")
host_tools.resolve = lambda *args, **kwargs: binary

push.run = harness_without_push
run = make_run(Path(sys.argv[2]))
run = dataclasses.replace(
    run, settings=dataclasses.replace(run.settings, kill_grace_seconds=0.2)
)
prepared = dataclasses.replace(
    preparation(run), auth_before='{"value":"old"}'
)
source = runfiles.private_dir(run.home / ".codex") / "auth.json"
source.write_text('{"value":"old"}')
intake.load = lambda args, tmpdir: (run, "example task")
intake.files_for = lambda name: run.files
process_group.prepare = lambda *args: prepared
process_group.boot_id = lambda: "example-boot"
original_launch = process_group.launch_owned
stub = """
import os, signal, sys
from pathlib import Path
child = os.fork()
if child == 0:
    signal.pause()
else:
    def finish(number, frame):
        os.waitpid(child, 0)
        sys.exit(0)
    signal.signal(signal.SIGTERM, finish)
    (Path(os.environ["CODEX_HOME"]) / "auth.json").write_text('{"value":"new"}')
    print(child, flush=True)
    signal.pause()
"""
if sys.argv[3] == "True":
    stub = """
import os, signal
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
(Path(os.environ["CODEX_HOME"]) / "auth.json").write_text('{"value":"new"}')
print(os.getpid(), flush=True)
signal.pause()
"""
    original_stop = process_group.stop_started
    def stop(pid, grace, *args):
        print("cleanup", flush=True)
        sys.stdin.readline()
        return original_stop(pid, grace, *args)
    process_group.stop_started = stop

def launch(spec, *, started):
    child = original_launch(
        dataclasses.replace(
            spec, command=(sys.executable, "-c", stub),
            streams=(None, subprocess.PIPE, None),
        ), started=started,
    )
    descendant = int(child.stdout.readline())
    (run.files.root / "descendant").write_text(str(descendant))
    return child
process_group.launch_owned = launch
write = runfiles.RunFiles.write
def recorded(self, suffix, text):
    write(self, suffix, text)
    if suffix == "phase" and text == "running":
        print("ready", flush=True)
runfiles.RunFiles.write = recorded
admission.ROOT = run.files.root / "admission"
sys.exit(dispatcher.main(["example"], "example run-host"))
'''
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(Path(__file__).resolve()),
            str(tmp_path),
            str(escalate),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert supervisor.stdout is not None
        assert supervisor.stdout.readline() == "ready\n"
        supervisor.send_signal(signal.SIGHUP)
        if escalate:
            assert supervisor.stdout.readline() == "cleanup\n"
            supervisor.send_signal(signal.SIGTERM)
            assert supervisor.stdin is not None
            supervisor.stdin.write("continue\n")
            supervisor.stdin.flush()
        output, error = supervisor.communicate(timeout=10)
        assert supervisor.returncode == 143, output + error
        files = runfiles.RunFiles(tmp_path / "runs", "example")
        assert files.path("rc").read_text() == "143\n"
        assert files.path("phase").read_text() == "ended"
        assert (
            tmp_path / "home" / ".codex" / "auth.json"
        ).read_text() == '{"value":"new"}'
        group = json.loads(files.path("pgid").read_text())["pgid"]
        descendant = int((files.root / "descendant").read_text())
        assert process_group.leader_start(group) is None
        assert process_group.leader_start(descendant) is None
        assert not process_group.alive(group)
    finally:
        group_file = tmp_path / "runs" / "example.pgid"
        if group_file.exists():
            recorded = Group(**json.loads(group_file.read_text()))
            # The fixture records a fake boot ID; match the actual leader's start.
            if process_group.leader_start(recorded.pgid) == recorded.start:
                process_group.stop_started(recorded.pgid, 0.2)
        if supervisor.poll() is None:
            supervisor.terminate()
        supervisor.communicate(timeout=10)


def test_host_help_names_program_and_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        host_dispatcher.main(["--help"], "example run-host")
    assert caught.value.code == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("usage: example run-host ")
    assert "--host HOST" in output.out
    assert "--prompt PROMPT" in output.out


@pytest.mark.parametrize("name", ["../unsafe", ""])
def test_invalid_name_has_no_completion_location(
    tmp_path: Path, config_file: Path, name: str
) -> None:
    config_file.write_text(EXAMPLE)
    assert host_dispatcher.main([name, "--host", "example-linux"], "example") == 1
    assert not list(tmp_path.rglob("*.rc"))


def test_load_interrupt_is_recorded_before_handlers_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load(_args: object, _tmpdir: Path) -> None:
        assert signal.getsignal(signal.SIGHUP) is host_dispatcher.interrupt
        os.kill(os.getpid(), signal.SIGHUP)

    monkeypatch.setattr(host_intake, "load", load)
    previous = signal.getsignal(signal.SIGHUP)
    assert host_dispatcher.main(["example"], "example") == 143
    root = Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev" / "agent-runs"
    assert (root / "example.rc").read_text() == "143\n"
    assert (
        root / "example.log"
    ).read_text() == "run refused: interrupted during load\n"
    assert signal.getsignal(signal.SIGHUP) == previous


def test_missing_name_exits_one_without_rc(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert host_dispatcher.main([], "example") == 1
    assert not list(tmp_path.rglob("*.rc"))
    assert capsys.readouterr() == ("", "example: run name is required\n")


def test_signal_after_the_run_is_recorded_keeps_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)

    @contextmanager
    def configured(*_args: object) -> Generator[None]:
        yield
        os.kill(os.getpid(), signal.SIGTERM)

    def execute(run: state.Run, *_args: object, **_kwargs: object) -> int:
        run.files.write("rc", "7\n")
        return 7

    monkeypatch.setattr(host_intake, "files_for", constant(run.files))
    monkeypatch.setattr(host_intake, "load", constant((run, "task")))
    monkeypatch.setattr(host_tools, "configured", configured)
    monkeypatch.setattr(host_execution, "_execute", execute)
    assert host_dispatcher.main(["example"], "example") == 7
    assert run.files.path("rc").read_text() == "7\n"


@pytest.mark.parametrize("request_file", [False, True])
@pytest.mark.parametrize("marker", ["rc", "pgid", "owner"])
def test_refused_rerun_preserves_every_file(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_file: bool,
    marker: str,
) -> None:
    config_file.write_text(EXAMPLE)
    files = runfiles.RunFiles(
        Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev/agent-runs", "example"
    )
    files.receive(run_request())
    files.write("prompt", "raw brief")
    files.write("log", "finished output\n")
    files.write(marker, "7\n")
    before = {p.name: p.read_bytes() for p in files.root.iterdir()}
    monkeypatch.setattr(host_execution, "execute", constant(99))
    arguments = ["example", "--host", "example-linux"]
    if request_file:
        arguments.insert(1, str(files.path("request.json")))
    assert host_dispatcher.main(arguments, "example run-host") == 1
    assert {p.name: p.read_bytes() for p in files.root.iterdir()} == before


@pytest.mark.parametrize("error", [OSError, RecursionError])
def test_late_temporary_cleanup_keeps_finished_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    run = make_run(tmp_path)
    cleanup = tempfile.TemporaryDirectory.cleanup

    def fail_cleanup(directory: tempfile.TemporaryDirectory[str]) -> None:
        cleanup(directory)
        raise error

    def execute(
        _run: state.Run,
        _text: str,
        *,
        preflight_only: bool = False,
    ) -> int:
        assert preflight_only is False
        run.files.write("log", "harness log\n")
        run.files.write("phase", "ended")
        run.files.write("rc", "7\n")
        return 7

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", fail_cleanup)
    monkeypatch.setattr(host_intake, "files_for", constant(run.files))
    monkeypatch.setattr(host_intake, "load", constant((run, "task")))
    monkeypatch.setattr(host_execution, "execute", execute)
    assert host_dispatcher.main(["example"], "example") == 7
    assert run.files.path("rc").read_text() == "7\n"
    assert run.files.path("phase").read_text() == "ended"
    assert run.files.path("log").read_text() == build_info.line() + "\nharness log\n"


@pytest.mark.parametrize("claimed", [False, True])
def test_main_claims_owner_before_loading(
    monkeypatch: pytest.MonkeyPatch,
    claimed: bool,
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )

    if claimed:
        files.write("owner", "host\n")
    calls: list[bool] = []

    def load(_args: object, _temporary: Path) -> tuple[state.Run, str]:
        assert files.path("owner").read_text() == "host\n"
        assert files.path("owner").stat().st_mode & 0o777 == 0o600
        calls.append(True)
        message = "test refusal"
        raise Invalid(message)

    monkeypatch.setattr(host_intake, "load", load)
    assert host_dispatcher.main(["example"], "run-host", claimed=claimed) == 1
    assert calls == [True]


@pytest.mark.parametrize("present", [True, False])
def test_main_with_owned_flag_requires_the_drivers_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    present: bool,
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )
    if present:
        files.write("owner", "run-id\n")
    calls: list[bool] = []

    def load(_args: object, _temporary: Path) -> tuple[state.Run, str]:
        calls.append(True)
        message = "test refusal"
        raise Invalid(message)

    monkeypatch.setattr(host_intake, "load", load)
    arguments = ["example", "--host", "h", "--owned"]
    assert host_dispatcher.main(arguments, "run-host") == 1
    if present:
        assert files.path("owner").read_text() == "run-id\n"
        assert calls == [True]
    else:
        assert calls == []
        assert "no usable owner file" in capsys.readouterr().err
        assert not files.path("log").exists()
        assert not files.path("rc").exists()


def test_main_with_request_refuses_an_existing_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )
    files.write("owner", "other\n")
    assert host_dispatcher.main(["example", "request.json"], "run-host") == 1
    assert "already has an owner" in capsys.readouterr().err
    assert not files.path("log").exists()
    assert not files.path("rc").exists()


def test_lock_acquisition_failure_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )
    files.write("log", "original output\n")

    def refuse(_path: Path) -> None:
        raise OSError(errno.EIO, "lock unavailable")

    monkeypatch.setattr(runfiles, "locked", refuse)
    assert host_dispatcher.main(["example"], "run-host") == 1
    assert "lock unavailable" in capsys.readouterr().err
    assert files.path("log").read_text() == "original output\n"
    assert not files.path("rc").exists()


def test_temp_directory_failure_leaves_log_and_rc(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )

    def full(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "temp directory unavailable")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", full)
    assert host_dispatcher.main(["example"], "run-host") == 1
    assert "temp directory unavailable" in capsys.readouterr().err
    assert "temp directory unavailable" in files.path("log").read_text()
    assert files.path("rc").read_text().strip() == "1"


def test_failure_records_are_written_while_the_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held: list[bool] = []
    state = {"held": False}
    real = runfiles.locked

    @contextmanager
    def tracking(path: Path) -> Generator[None]:
        with real(path):
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

    real_write = runfiles.RunFiles.write

    def write(self: runfiles.RunFiles, suffix: str, text: str) -> None:
        held.append(state["held"])
        real_write(self, suffix, text)

    def load(_args: object, _tmpdir: Path) -> None:
        os.kill(os.getpid(), signal.SIGHUP)

    monkeypatch.setattr(runfiles, "locked", tracking)
    monkeypatch.setattr(runfiles.RunFiles, "write", write)
    monkeypatch.setattr(host_intake, "load", load)
    assert host_dispatcher.main(["example"], "run-host") == 143
    assert held == [True, True]


def test_interrupted_second_admission_leaves_the_completed_run_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )
    real = host_dispatcher.refuse_started
    calls: list[int] = []

    def admit(checked: runfiles.RunFiles) -> None:
        calls.append(1)
        if len(calls) == 1:
            return
        # The earlier run finished while this one waited for the lock.
        files.write("log", "original output\n")
        files.write("rc", "0\n")
        try:
            real(checked)
        except RunExistsError:
            raise state.RunInterruptedError from None

    monkeypatch.setattr(host_dispatcher, "refuse_started", admit)
    assert host_dispatcher.main(["example"], "run-host") == 143
    assert files.path("log").read_text() == "original output\n"
    assert files.path("rc").read_text() == "0\n"


@pytest.mark.parametrize("expected", [1, 7])
def test_interrupt_while_releasing_the_lock_keeps_the_recorded_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected: int
) -> None:
    run = make_run(tmp_path)

    @contextmanager
    def interrupted_release(_path: Path) -> Generator[None]:
        try:
            yield
        finally:
            raise state.RunInterruptedError

    def load(_args: object, _tmpdir: Path) -> tuple[state.Run, str]:
        if expected == 1:
            raise OSError(errno.EIO, "load unavailable")
        return run, "example task"

    def execute(*_args: object, **_kwargs: object) -> int:
        run.files.write("rc", "7\n")
        return 7

    monkeypatch.setattr(host_intake, "files_for", constant(run.files))
    monkeypatch.setattr(runfiles, "locked", interrupted_release)
    monkeypatch.setattr(host_intake, "load", load)
    monkeypatch.setattr(host_execution, "execute", execute)
    assert host_dispatcher.main(["example"], "run-host") == expected
    assert run.files.path("rc").read_text() == f"{expected}\n"


def test_signal_after_recording_a_failure_keeps_the_recorded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )
    record = host_dispatcher._record_failure  # noqa: SLF001

    def record_then_terminate(files: runfiles.RunFiles, exc: Exception) -> None:
        record(files, exc)
        os.kill(os.getpid(), signal.SIGTERM)

    def load(_args: object, _tmpdir: Path) -> None:
        raise OSError(errno.EIO, "load unavailable")

    monkeypatch.setattr(host_dispatcher, "_record_failure", record_then_terminate)
    monkeypatch.setattr(host_intake, "load", load)
    assert host_dispatcher.main(["example"], "run-host") == 1
    assert files.path("rc").read_text().strip() == "1"


def test_waiter_interrupted_before_the_lock_leaves_the_run_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )
    files.write("log", "original output\n")

    class Waiting:
        def __enter__(self) -> None:
            raise state.RunInterruptedError

        def __exit__(self, *_exception: object) -> None:
            return None

    def waiting(_path: Path) -> Waiting:
        return Waiting()

    monkeypatch.setattr(runfiles, "locked", waiting)
    assert host_dispatcher.main(["example"], "run-host") == 143
    assert files.path("log").read_text() == "original output\n"
    assert not files.path("rc").exists()


@pytest.mark.parametrize("present", [False, True])
def test_main_logs_build_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    present: bool,
) -> None:
    run = make_run(tmp_path)
    expected = build_provenance(tmp_path, monkeypatch, present=present)
    monkeypatch.setattr(host_intake, "load", constant((run, "task")))
    monkeypatch.setattr(host_execution, "execute", constant(7))
    assert host_dispatcher.main(["example"], "run-host") == 7
    assert run.files.path("log").read_text() == (
        f"tbhbdev tools_commit={expected['tools_commit']} "
        f"wheel_commit={expected['wheel_commit']}\n"
    )


def test_preflight_flag_reaches_execute(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file.write_text(EXAMPLE)
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev" / "agent-runs", "example"
    )
    files.receive(run_request())
    files.write("prompt", "example")

    def execute(
        run: state.Run,
        text: str,
        *,
        preflight_only: bool = False,
    ) -> int:
        assert run.files.name == "example"
        assert text == "example"
        assert preflight_only is True
        return 0

    monkeypatch.setattr(host_execution, "execute", execute)
    assert (
        host_dispatcher.main(
            ["example", "--host", "example-linux", "--preflight"], "run-host"
        )
        == 0
    )
