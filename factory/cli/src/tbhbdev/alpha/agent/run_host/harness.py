"""Launch the harness and shut down its process group."""

from __future__ import annotations

import json
import subprocess  # nosec B404
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import run_stop_hook
from tbhbdev.alpha.agent import app_server, bazel_servers, process_group, runfiles
from tbhbdev.alpha.agent.decisions import run_state
from tbhbdev.alpha.agent.model import AgyPolicy, Harness, Policy, RunPaths
from tbhbdev.alpha.agent.run_host import (
    bazel_shutdown,
    command_line,
    preflight,
    telemetry,
)
from tbhbdev.alpha.identity.commit.policy import RUN_MODEL_ENV
from tbhbdev.alpha.install.hooks import stop_hooks

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import Prepared, Run


def run_harness(
    run: Run, prepared: Prepared, stack: ExitStack, *, preflight_only: bool = False
) -> int:
    paths = prepared.paths
    value, executable = preflight.resolve_harness(run, paths)
    env = (
        value.env
        | prepared.delivery.env
        | telemetry.resource_attributes(
            run.settings.otel_endpoint,
            host=run.host.name,
            account=run.request.account,
            run_name=run.files.name,
        )
        | {RUN_MODEL_ENV: run.request.model}
    )
    command = harness_command(run, prepared, value, executable)
    if preflight_only:
        log_hook_tools(run, paths)
        run.files.write("log", f"preflight ok: {executable}\n")
        return 0
    log_argv(run, command)
    if run.steer:
        socket = app_server.socket_path(run.files.root, run.files.name)
        command = app_server.launch_command(command, socket)
    context = run_stop_hook.run_context(
        run.request, paths.worktree, run.files, run.tmpdir, prepared.start
    )
    env[run_stop_hook.ENV] = json.dumps(context)
    streams = (
        stack.enter_context(run.files.path("prompt").open()),
        stack.enter_context(run.files.path("out").open("w")),
        stack.enter_context(run.files.path("log").open("a")),
    )
    if time.monotonic() >= run.deadline:
        raise TimeoutError
    child = None
    launched = 0.0

    def started(value: subprocess.Popen[str]) -> None:
        nonlocal child, launched
        child = value
        launched = time.monotonic()
        run.setup_seconds = launched - run.begun
        process_group.record(run.files, child.pid)
        run.files.write("launched", "\n")

    try:
        with process_group.defer_signals():
            child = process_group.launch_owned(
                process_group.Launch(command, env, paths.worktree, streams),
                started=started,
            )
        run.files.write("phase", "running")
        return wait_harness(run, child)
    finally:
        if child is not None:
            run.waited = time.monotonic()
            run.model_seconds = run.waited - launched
        stack.enter_context(process_group.ignore_signals())
        if child is not None:
            stopped = _stop_harness(run, prepared, child)
            if stopped:
                with suppress(OSError, subprocess.SubprocessError):
                    child.wait()
                    run.files.write("phase", "stopped")
            if stopped and run.request.harness is Harness.CODEX:
                with suppress(FileNotFoundError):
                    runfiles.copy_final(
                        run.tmpdir / "out.final", run.files.path("out.final")
                    )


def harness_command(
    run: Run, prepared: Prepared, value: Policy, executable: str
) -> tuple[str, ...]:
    paths = prepared.paths
    text = run.files.path("prompt").read_text()
    project = (
        _agy_project(run, prepared, value.detail)
        if isinstance(value.detail, AgyPolicy)
        else None
    )
    schema = run.files.path("schema.json")
    schema_text = schema.read_text() if schema.is_file() else None
    return command_line.harness(
        run.request,
        value,
        worktree=paths.worktree,
        executable=executable,
        final_message=str(run.tmpdir / "out.final"),
        stop_hooks=stop_hooks("claude")
        if run.request.harness is Harness.CLAUDE
        else {},
        agy_project=project,
        agy_brief=text,
        schema_file=str(schema) if schema_text is not None else None,
        schema_text=schema_text,
    )


def _agy_project(run: Run, prepared: Prepared, grants: AgyPolicy) -> str:
    """Keep the run's project and its grants inside its private temp directory."""
    path = run.tmpdir / "agy-project.json"
    runfiles.atomic(
        path,
        command_line.agy_project(
            run.files.name,
            prepared.paths.worktree,
            grants.grants,
            run.settings.agy_timeout_seconds,
        ),
    )
    return str(path)


def log_hook_tools(run: Run, paths: RunPaths) -> None:
    status = (
        "present"
        if (Path(paths.worktree) / ".bazel_env" / "bin").is_dir()
        else "missing; commit hooks that call .bazel_env/bin will fail in this run"
    )
    run.files.write("log", f"preflight: .bazel_env {status}\n")


def log_argv(run: Run, command: tuple[str, ...]) -> None:
    if run.request.harness is Harness.CODEX:
        # A failed log write must not stop the run, as with the cleanup notices.
        with suppress(OSError):
            run.files.write("log", telemetry.argv_log_line(command) + "\n")


def wait_harness(run: Run, child: subprocess.Popen[str]) -> int:
    limit = run_state.time_limit(run.request.timeout, run.settings.max_run_minutes)
    try:
        rc = run_state.exit_status(child.wait(timeout=limit))
    except subprocess.TimeoutExpired:
        rc = 124
    if rc:
        with suppress(OSError):
            run.files.write("log", f"harness exited {rc}\n")
    return rc


def _stop_harness(run: Run, prepared: Prepared, child: subprocess.Popen[str]) -> bool:
    with suppress(OSError):
        run.files.write("phase", "exited")
    # Capture ancestry before stopping the harness. Stop Bazel afterwards so
    # the harness cannot restart its server during cleanup.
    servers = None
    with suppress(Exception):
        servers = bazel_servers.find(Path(prepared.paths.worktree), child.pid)
    stopped = process_group.stop_owned(
        run.files, child.pid, run.settings.kill_grace_seconds, child.poll
    )
    with suppress(Exception):
        run.bazel_shutdown = bazel_shutdown.shutdown_bazel(
            run, prepared, servers=servers
        )
    return stopped
