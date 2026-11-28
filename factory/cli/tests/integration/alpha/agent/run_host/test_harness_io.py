"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import TextIO, cast
lazy from collections.abc import Callable

import pytest
from agent_host_example import (
    constant,
    host_settings,
    make_run,
)
from run_host_fixtures import (
    HOST_FIXTURES,
    Child,
    bazel_run,
    preparation,
)

from tbhbdev.alpha import run_stop_hook
from tbhbdev.alpha.agent import (
    bazel_servers,
    credentials,
    host_tools,
    process_group,
    render,
    runfiles,
)
from tbhbdev.alpha.agent.model import (
    Harness,
    Invalid,
    Os,
)
from tbhbdev.alpha.agent.run_host import bazel_shutdown as host_bazel_shutdown
from tbhbdev.alpha.agent.run_host import dispatcher as host_dispatcher
from tbhbdev.alpha.agent.run_host import environment as host_environment
from tbhbdev.alpha.agent.run_host import execution as host_execution
from tbhbdev.alpha.agent.run_host import harness as host_harness
from tbhbdev.alpha.agent.run_host import preflight as host_preflight
from tbhbdev.alpha.agent.run_host import state
from tbhbdev.alpha.identity.commit.policy import RUN_MODEL_ENV

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


def test_hook_tools_checks_the_exact_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    expected = Path(prepared.paths.worktree) / ".bazel_env" / "bin"
    checked: list[Path] = []
    original = Path.is_dir

    def is_dir(path: Path) -> bool:
        checked.append(path)
        return original(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    host_harness.log_hook_tools(run, prepared.paths)
    assert expected in checked


@pytest.mark.parametrize("harness", Harness)
def test_launch_uses_the_policy_and_marks_only_after_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        timeout=900,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example" if harness is Harness.CLAUDE else None,
    )
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, max_run_minutes=1)
    )
    prepared = preparation(run)
    env = (
        {credentials.CLAUDE_TOKEN_ENV: "example-claude"}
        if harness is Harness.CLAUDE
        else {}
    )
    prepared = dataclasses.replace(
        prepared,
        delivery=credentials.Delivery(
            env, None, claude_profile="primary" if env else None
        ),
    )
    run.files.write("prompt", render.brief("example task"))
    metadata: list[tuple[str, str]] = []
    real_write = run.files.write

    def write(suffix: str, text: str) -> None:
        metadata.append((suffix, text))
        real_write(suffix, text)

    monkeypatch.setattr(run.files, "write", write)
    child = Child()
    events: list[object] = []
    monkeypatch.setenv("EXAMPLE_PARENT_MARKER", "example-parent-credential")
    monkeypatch.setenv("EXAMPLE_TMUX_MARKER", "example-tmux-credential")

    def launch(spec: process_group.Launch, **kwargs: object) -> Child:
        check_launch_contract(run, prepared, spec)
        events.append("spawn")
        cast("Callable[[Child], None]", kwargs["started"])(child)
        return child

    def record(files: runfiles.RunFiles, pid: int) -> None:
        assert files is run.files
        assert pid == 23
        assert not files.path("launched").exists()
        events.append("record")

    def stop(pid: int, grace: float, *_args: object) -> None:
        assert (pid, grace) == (23, 0)
        assert run.files.path("launched").exists()
        # Bazel servers shut down only after the harness group has stopped.
        assert (run.files.path("phase").read_text(), run.bazel_shutdown) == (
            "exited",
            None,
        )
        events.append("stop")

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", record)
    monkeypatch.setattr(process_group, "stop_started", stop)
    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    with ExitStack() as stack:
        assert host_harness.run_harness(run, prepared, stack) == 7
    assert run.files.path("phase").read_text() == "stopped"
    assert events == [
        "spawn",
        "record",
        "stop",
    ]
    assert [entry for entry in metadata if not entry[1].startswith("argv: ")] == [
        ("launched", "\n"),
        ("phase", "running"),
        ("log", "harness exited 7\n"),
        ("phase", "exited"),
        ("phase", "stopped"),
    ]
    assert not any("preflight:" in text for suffix, text in metadata if suffix == "log")
    assert child.waits == [60, None]
    assert run.files.path("out").exists()


@pytest.mark.parametrize("expired", [False, True])
def test_harness_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    run = make_run(tmp_path, timeout=1)
    run = dataclasses.replace(run, deadline=1000 if expired else 1001)
    monkeypatch.setattr(time, "monotonic", lambda: 1000)
    prepared = preparation(run)
    run.files.write("prompt", "example")
    child = Child(timeout=True)

    def launch(*_args: object, **kwargs: object) -> Child:
        cast("Callable[[Child], None]", kwargs["started"])(child)
        return child

    def record(_files: runfiles.RunFiles, _pid: int) -> None:
        return None

    stopped: list[int] = []

    def stop(pid: int, _grace: float, *_args: object) -> None:
        stopped.append(pid)

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", record)
    monkeypatch.setattr(process_group, "stop_started", stop)
    with ExitStack() as stack:
        if expired:
            with pytest.raises(TimeoutError):
                host_harness.run_harness(run, prepared, stack)
            assert stopped == []
        else:
            assert host_harness.run_harness(run, prepared, stack) == 124
            assert stopped == [23]
            assert "harness exited 124\n" in run.files.path("log").read_text()


def tmux_environment_probe(root: Path, harness: Harness) -> None:
    run = make_run(
        root,
        harness=harness.value,
        timeout=900,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example" if harness is Harness.CLAUDE else None,
    )
    paths = preparation(run).paths
    env = host_environment.environment(
        run.host, harness, paths, run.request.account, host_tools.search()
    )
    if harness is Harness.CLAUDE:
        env[credentials.CLAUDE_TOKEN_ENV] = "example-claude"
    env["PYTHONCOERCECLOCALE"] = "0"
    command = (
        sys.executable,
        "-c",
        (
            "import os; os.environ.pop('__CF_USER_TEXT_ENCODING', None); "
            'print("\\n".join(k + "=" + v for k, v in os.environ.items()))'
        ),
    )
    child = process_group.launch(
        command,
        env=env,
        cwd=str(root),
        streams=(None, subprocess.PIPE, subprocess.PIPE),
    )
    output, error = child.communicate(timeout=5)
    assert child.returncode == 0, error
    actual = dict(line.split("=", 1) for line in output.splitlines())
    assert actual.keys() == env.keys(), actual.keys() - env.keys()
    assert actual == env
    assert os.environ["EXAMPLE_PARENT_MARKER"] == "parent-marker"
    assert os.environ["EXAMPLE_TMUX_MARKER"] == "server-marker"
    (root / "result").write_text("isolated")


@pytest.mark.parametrize("harness", Harness)
def test_tmux_server_and_parent_markers_do_not_reach_a_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    operating_system = Os.MACOS if sys.platform == "darwin" else Os.LINUX
    with host_tools.configured(host_settings(), operating_system):
        try:
            tmux = host_tools.resolve("tmux")
        except Invalid:
            pytest.skip("tmux is unavailable in the production host search path")
    monkeypatch.setenv("EXAMPLE_PARENT_MARKER", "parent-marker")
    loader = (
        "import runpy, sys, subprocess, traceback\n"
        "from pathlib import Path\n"
        "try:\n"
        "    sys.path.insert(0, str(Path(sys.argv[1]).parent))\n"
        "    m=runpy.run_path(sys.argv[1])\n"
        f"    fn=m['{tmux_environment_probe.__name__}']\n"
        "    fn(Path(sys.argv[2]), m['Harness'](sys.argv[3]))\n"
        "except BaseException:\n"
        "    (Path(sys.argv[2]) / 'error').write_text(traceback.format_exc())\n"
        "finally:\n"
        "    subprocess.run([sys.argv[5], '-S', sys.argv[4], 'wait-for', '-S', "
        "'complete'], check=True)\n"
    )
    with tempfile.TemporaryDirectory(prefix="ag-tmux-") as directory:
        socket_path = str(Path(directory) / "socket")
        base = [tmux, "-S", socket_path]
        subprocess.run(
            [
                *base,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "anchor",
                "sleep",
                "30",
            ],
            check=True,
            timeout=5,
        )
        try:
            subprocess.run(
                [
                    *base,
                    "set-environment",
                    "-g",
                    "EXAMPLE_TMUX_MARKER",
                    "server-marker",
                ],
                check=True,
                timeout=5,
            )
            subprocess.run(
                [
                    *base,
                    "new-session",
                    "-d",
                    "-s",
                    "probe",
                    sys.executable,
                    "-c",
                    loader,
                    str(Path(__file__).resolve()),
                    str(tmp_path),
                    harness.value,
                    socket_path,
                    tmux,
                ],
                check=True,
                timeout=5,
            )
            subprocess.run([*base, "wait-for", "complete"], check=True, timeout=10)
            assert (tmp_path / "result").exists(), (tmp_path / "error").read_text()
            assert (tmp_path / "result").read_text() == "isolated"
        finally:
            subprocess.run([*base, "kill-server"], check=False, timeout=5)


def test_signal_at_launch_handoff_records_and_stops_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    child = Child()
    events: list[str] = []

    def launch(*_args: object, **kwargs: object) -> Child:
        cast("Callable[[Child], None]", kwargs["started"])(child)
        os.kill(os.getpid(), signal.SIGHUP)
        return child

    def record(files: runfiles.RunFiles, pid: int) -> None:
        assert pid == child.pid
        files.write("pgid", "recorded")
        events.append("recorded")

    def stop(pid: int, grace: float, *_args: object) -> None:
        assert (pid, grace) == (child.pid, 0)
        events.append("stopped")
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", record)
    monkeypatch.setattr(process_group, "stop_started", stop)
    with process_group.defer_signals():
        signal.signal(signal.SIGHUP, host_dispatcher.interrupt)
        signal.signal(signal.SIGTERM, host_dispatcher.interrupt)
        assert host_execution.execute(run, "example") == 143
    assert events == ["recorded", "stopped"]
    assert child.waits == [None]
    assert run.files.path("rc").read_text() == "143\n"


@pytest.mark.parametrize("steer", [False, True])
def test_final_answer_is_copied_only_after_child_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steer: bool
) -> None:
    run = make_run(tmp_path)
    run.steer = steer
    run.files.write("prompt", "task")
    run.files.receive(run.request)
    prepared = preparation(run)
    child = Child()
    marker = run.tmpdir / "executed"

    def launch(
        spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        if steer:
            assert "app-server --listen unix://" in spec.command[-1]
            assert "--remote unix://" in spec.command[-1]
            assert "model=" in spec.command[-1]
        else:
            assert spec.command[-2] == str(run.tmpdir / "out.final")
        assert not run.files.path("out.final").exists()
        (run.tmpdir / "out.final").write_text("answer")
        run.files.path("request.json").write_text(
            f"__import__('pathlib').Path({str(marker)!r}).write_text('escaped')"
        )
        started(child)
        return child

    def stop(*_args: object) -> None:
        assert not run.files.path("out.final").exists()

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", stop)
    with ExitStack() as stack:
        assert host_harness.run_harness(run, prepared, stack) == 7
    assert run.files.path("out.final").read_text() == "answer"
    assert not marker.exists()


@pytest.mark.parametrize(
    "harness",
    [Harness.CLAUDE, Harness.AGY],
)
def test_only_headless_codex_can_supply_final_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        timeout=900,
        reason="test",
        claude_profile="primary" if harness is Harness.CLAUDE else None,
    )
    run.files.write("prompt", "task")
    (run.tmpdir / "out.final").write_text("unused")
    child = Child()

    def launch(
        _spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        started(child)
        return child

    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", constant(None))
    with ExitStack() as stack:
        assert host_harness.run_harness(run, preparation(run), stack) == 7
    assert not run.files.path("out.final").exists()


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_codex_argv_is_logged_without_environment_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        reason="test",
        claude_profile="primary" if harness is Harness.CLAUDE else None,
    )
    run.files.write("prompt", "raw brief")
    prepared = preparation(run)
    value, _ = host_preflight.resolve_harness(run, prepared.paths)

    class Child:
        pid = 99124

        def poll(self) -> int:
            return 0

        def wait(self, **_kwargs: object) -> int:
            return 0

    def launch(
        _spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        child = Child()
        started(child)
        return child

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", constant(None))
    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    with ExitStack() as stack:
        assert host_harness.run_harness(run, prepared, stack) == 0
    log = run.files.path("log").read_text()
    if harness is not Harness.CODEX:
        assert "argv:" not in log
        return
    assert log.startswith("argv: ")
    assert log.count("argv: ") == 1
    assert "danger-full-access" in log
    assert "raw brief" not in log
    assert "shell_environment_policy.set." not in log
    assert value.tool_path not in log
    assert "shell_environment_policy.exclude=" in log


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_log_argv_writes_one_complete_line_only_for_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example",
    )
    writes: list[tuple[str, str]] = []

    def write(suffix: str, text: str) -> None:
        writes.append((suffix, text))

    monkeypatch.setattr(run.files, "write", write)
    host_harness.log_argv(run, ("codex", "a b"))
    assert writes == (
        [("log", "argv: codex 'a b'\n")] if harness is Harness.CODEX else []
    )


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_driver_schema_file_reaches_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        reason="test",
        claude_profile="primary" if harness is Harness.CLAUDE else None,
    )
    schema = '{"type":"object","properties":{"answer":{"type":"string"}}}'
    run.files.write("schema.json", schema)
    run.files.write("prompt", "raw brief")
    prepared = preparation(run)
    commands: list[tuple[str, ...]] = []

    class Child:
        pid = 99123

        def poll(self) -> int:
            return 0

        def wait(self, **_kwargs: object) -> int:
            return 0

    def launch(
        spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        commands.append(spec.command)
        child = Child()
        started(child)
        return child

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", constant(None))
    with ExitStack() as stack:
        assert host_harness.run_harness(run, prepared, stack) == 0
    command = commands[0]
    if harness is Harness.CODEX:
        assert command[command.index("--output-schema") + 1] == str(
            run.files.path("schema.json")
        )
    else:
        assert command[command.index("--json-schema") + 1] == schema
        assert command[command.index("--output-format") + 1] == "json"


@pytest.mark.parametrize("failure", ["exited", "stopped", "wait-os", "wait-subprocess"])
def test_harness_cleanup_errors_keep_the_observed_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    run = make_run(tmp_path, timeout=60)
    prepared = preparation(run)
    run.files.write("prompt", "probe")
    child = Child()
    original = run.files.write
    stops: list[int] = []

    def launch(
        _spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        started(child)
        return child

    def wait(timeout: int | None = None) -> int:
        if timeout is None:
            if failure == "wait-os":
                raise OSError
            if failure == "wait-subprocess":
                raise subprocess.SubprocessError
        return 7

    def write(suffix: str, text: str) -> None:
        if suffix == "phase" and text == failure:
            raise OSError
        original(suffix, text)

    def stop(
        files: runfiles.RunFiles,
        pid: int,
        grace: float,
        reap: Callable[[], object] | None = None,
    ) -> bool:
        assert files is run.files
        assert (pid, grace) == (child.pid, run.settings.kill_grace_seconds)
        assert reap == child.poll
        stops.append(pid)
        return True

    monkeypatch.setattr(child, "wait", wait)
    monkeypatch.setattr(run.files, "write", write)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "stop_owned", stop)
    with ExitStack() as stack:
        assert host_harness.run_harness(run, prepared, stack) == 7
    assert stops == [child.pid]


def test_stop_harness_shuts_servers_down_after_stopping_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    worktree = Path(prepared.paths.worktree)
    server = bazel_servers.Server(41, belongs=True, started="start")
    base = tmp_path / "base"
    events: list[str] = []

    def find(found_worktree: Path, group: int) -> dict[Path, bazel_servers.Server]:
        assert (found_worktree, group) == (worktree, 23)
        events.append("find")
        return {base: server}

    def stop(*_args: object) -> bool:
        events.append("stop")
        return True

    def shutdown(
        _run: state.Run,
        _prepared: state.Prepared,
        *,
        servers: dict[Path, bazel_servers.Server],
    ) -> state.BazelShutdown:
        assert servers == {base: server}
        events.append("shutdown")
        return {}

    monkeypatch.setattr(bazel_servers, "find", find)
    monkeypatch.setattr(process_group, "stop_owned", stop)
    monkeypatch.setattr(host_bazel_shutdown, "shutdown_bazel", shutdown)
    child = cast("subprocess.Popen[str]", Child())
    assert host_harness._stop_harness(run, prepared, child) is True  # noqa: SLF001
    assert events == ["find", "stop", "shutdown"]
    assert run.bazel_shutdown == {}


def check_launch_contract(
    run: state.Run, prepared: state.Prepared, spec: process_group.Launch
) -> None:
    assert not run.files.path("launched").exists()
    assert spec.cwd == prepared.paths.worktree
    assert spec.command[0] == str(run.home.parent / "host-bin" / "true")
    assert "EXAMPLE_PARENT_MARKER" not in spec.env
    assert "EXAMPLE_TMUX_MARKER" not in spec.env
    assert spec.env[RUN_MODEL_ENV] == run.request.model
    assert json.loads(spec.env[run_stop_hook.ENV])["workspace"] == spec.cwd
    assert "example-claude" not in repr(spec.command)
    stdin, stdout, stderr = cast("tuple[TextIO, TextIO, TextIO]", spec.streams)
    assert stdin.read() == render.brief("example task")
    assert stdout.name == str(run.files.path("out"))
    assert stderr.name == str(run.files.path("log"))
    if run.request.harness is Harness.AGY:
        project = json.loads((run.tmpdir / "agy-project.json").read_text())
        assert project == {
            "name": "agent-example",
            "path": prepared.paths.worktree,
            "permissions": [],
            "timeout": 900,
        }
        assert spec.command[spec.command.index("--project") + 1] == str(
            run.tmpdir / "agy-project.json"
        )
    if run.request.harness is Harness.CODEX:
        assert spec.command[-3:] == (
            "--output-last-message",
            str(run.tmpdir / "out.final"),
            "-",
        )
    else:
        assert "--output-last-message" not in spec.command


def test_run_harness_serves_gh_refresh_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    gh_dir = runfiles.private_dir(run.tmpdir / "gh")
    runfiles.atomic(gh_dir / "hosts.yml", "github.com:\n  oauth_token: launch\n")
    delivery = credentials.Delivery(
        {}, None, None, credentials.StoredToken("launch", 1000.0)
    )
    prepared = dataclasses.replace(preparation(run), delivery=delivery)
    run.files.write("prompt", "example")
    seen: list[bool] = []
    child = Child(code=0)

    def launch(*_args: object, **kwargs: object) -> Child:
        seen.append((gh_dir / "token.sock").is_socket())
        cast("Callable[[Child], None]", kwargs["started"])(child)
        return child

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", lambda *_args: None)
    monkeypatch.setattr(process_group, "stop_started", lambda *_args: None)
    with ExitStack() as stack:
        assert host_harness.run_harness(run, prepared, stack) == 0
    assert seen == [True]
    assert not (gh_dir / "token.sock").exists()
