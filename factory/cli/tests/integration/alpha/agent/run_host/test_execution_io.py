"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest
from agent_host_example import (
    constant,
    local_mirror_fetches,
    make_run,
    repository,
)
from run_host_fixtures import (
    HOST_FIXTURES,
    Child,
    HostRuntime,
    preparation,
)

from tbhbdev import secrets
from tbhbdev.alpha.agent import (
    credentials,
    process_group,
    push,
    render,
    runfiles,
    shims,
    worktree,
)
from tbhbdev.alpha.agent.model import (
    Harness,
    Invalid,
    Policy,
)
from tbhbdev.alpha.agent.run_host import auth as host_auth
from tbhbdev.alpha.agent.run_host import bazel_shutdown as host_bazel_shutdown
from tbhbdev.alpha.agent.run_host import dispatcher as host_dispatcher
from tbhbdev.alpha.agent.run_host import execution as host_execution
from tbhbdev.alpha.agent.run_host import harness as host_harness
from tbhbdev.alpha.agent.run_host import intake as host_intake
from tbhbdev.alpha.agent.run_host import preflight as host_preflight
from tbhbdev.alpha.agent.run_host import state
from tbhbdev.alpha.agent.run_host import workspace as host_workspace

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


@pytest.mark.parametrize("hook_tools", ["missing", "directory", "file"])
def test_preflight_prepares_and_stops_signing_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_tools: str
) -> None:
    run = make_run(tmp_path)
    (run.home / ".bazel_env" / "bin").mkdir(parents=True)
    prepared = dataclasses.replace(
        preparation(run),
        delivery=credentials.Delivery({}, "example-key", claude_profile=None),
    )
    events: list[str] = []
    original_harness = host_harness.run_harness

    def harness(
        value: state.Run,
        ready: state.Prepared,
        stack: ExitStack,
        *,
        preflight_only: bool = False,
    ) -> int:
        assert isinstance(stack, ExitStack)
        assert preflight_only is True
        return original_harness(value, ready, stack, preflight_only=preflight_only)

    def prepare(operation: Callable[[], object], *_args: object) -> object:
        events.append("prepare")
        return operation()

    def workspace(value: state.Run) -> state.Prepared:
        assert value is run
        events.append("workspace")
        tools = Path(prepared.paths.worktree) / ".bazel_env" / "bin"
        tools.parent.mkdir()
        if hook_tools == "directory":
            tools.mkdir()
        elif hook_tools == "file":
            tools.touch()
        return prepared

    def sign(key: str, tmpdir: Path, deadline: float) -> Child:
        assert (key, tmpdir, deadline) == ("example-key", run.tmpdir, run.deadline)
        events.append("sign")
        return Child()

    def stop(child: Child) -> bool:
        assert child.pid == Child.pid
        events.append("stop")
        return True

    def command(
        value: state.Run, ready: state.Prepared, policy: Policy, executable: str
    ) -> tuple[str, ...]:
        assert value is run
        assert ready is prepared
        assert policy.env
        assert Path(executable).is_absolute()
        events.append("command")
        return (executable,)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("preflight must never launch or push")

    monkeypatch.setattr(process_group, "prepare", prepare)
    monkeypatch.setattr(host_workspace, "prepare_workspace", workspace)
    monkeypatch.setattr(host_harness, "run_harness", harness)
    monkeypatch.setattr(credentials, "signing_agent", sign)
    monkeypatch.setattr(host_auth, "stop_agent", stop)
    monkeypatch.setattr(host_harness, "harness_command", command)
    monkeypatch.setattr(process_group, "launch_owned", unexpected)
    monkeypatch.setattr(push, "run", unexpected)
    assert host_execution.execute(run, "example", preflight_only=True) == 0
    assert events == ["prepare", "workspace", "sign", "command", "stop"]
    status = (
        "present"
        if hook_tools == "directory"
        else "missing; commit hooks that call .bazel_env/bin will fail in this run"
    )
    assert run.files.path("log").read_text() == (
        "unsandboxed full access authorized_by=None\n"
        f"preflight: .bazel_env {status}\n"
        f"preflight ok: {run.home.parent / 'host-bin' / 'true'}\n"
    )
    assert run.files.path("rc").read_text() == "0\n"
    assert run.files.path("phase").read_text() == "ended"
    assert runfiles.latest(run.files.root)["example"]["rc"] == 0
    assert not run.files.path("launched").exists()


@pytest.mark.parametrize("phase", ["fetch", "secret"])
def test_startup_deadline_stops_the_recorded_setup_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    run = make_run(tmp_path)
    _, clone = repository(run.home / "Code" / "github.com" / "tbhb-dev")
    clone.rename(clone.parent / "example-repo")
    local_mirror_fetches(monkeypatch, clone.parent / "origin")
    original = worktree.git

    def hang() -> str:
        code = (
            "import json, os, signal, sys; "
            "row = json.dumps([os.getpid(), os.getpgrp()]); "
            "os.write(int(sys.argv[1]), row.encode() + b'\\n'); "
            "signal.pause()"
        )
        return subprocess.check_output(
            [sys.executable, "-c", code, str(ready_write)],
            pass_fds=(ready_write,),
            text=True,
        )

    def clock() -> float:
        if not run.files.path("pgid").exists():
            return time.monotonic()
        assert select.select([ready_read], [], [], 5)[0], "setup child did not start"
        return run.deadline

    def git(path: Path, *args: str) -> str:
        return (
            hang()
            if phase == "fetch" and args[0] in ("fetch", "ls-remote")
            else original(path, *args)
        )

    def read(_reference: str) -> str:
        return hang()

    def boot() -> str:
        return "example-boot"

    def skip_verification(_workspace: Path, _env: Mapping[str, str]) -> None:
        return None

    monkeypatch.setattr(process_group, "boot_id", boot)
    monkeypatch.setattr(worktree, "git", git)
    monkeypatch.setattr(worktree, "verify_run_hooks", skip_verification)
    if phase == "secret":
        prepared = preparation(run)
        monkeypatch.setattr(worktree, "create", constant(Path(prepared.paths.worktree)))
        monkeypatch.setattr(secrets, "read", read)
    ready_read, ready_write = os.pipe()
    with os.fdopen(ready_read, "rb") as ready, os.fdopen(ready_write, "wb"):
        monkeypatch.setattr(
            process_group, "prepare", partial(process_group.prepare, clock=clock)
        )
        assert host_execution.execute(run, "example") == 124
        pid, group = json.loads(ready.readline())
    assert run.files.path("rc").read_text() == "124\n"
    assert not run.files.path("launched").exists()
    assert json.loads(run.files.path("pgid").read_text())["pgid"] == group
    assert process_group.leader_start(pid) is None
    assert process_group.leader_start(group) is None


def test_claude_review_reaches_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(
        tmp_path,
        harness="claude",
        profile="review",
        commit="0" * 40,
        timeout=900,
        claude_profile="primary",
        reason="example review",
    )

    reached = []

    def prepare(*_args: object) -> None:
        reached.append(True)
        message = "preparation reached"
        raise Invalid(message)

    monkeypatch.setattr(process_group, "prepare", prepare)
    run.files.receive(run.request)
    assert host_execution.execute(run, "example") == 1
    assert run.files.path("rc").read_text() == "1\n"
    assert reached == [True]
    assert "preparation reached" in run.files.path("log").read_text()
    assert not (run.tmpdir / "ssh.sock").exists()
    assert not (run.home / ".codex").exists()
    assert runfiles.latest(run.files.root)["example"]["state"] == "ended"


def test_failed_launch_still_records_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    run.files.receive(run.request)
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))

    def launch(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(process_group, "launch_owned", launch)
    assert host_execution.execute(run, "example") == 1
    assert run.files.path("rc").read_text() == "1\n"
    assert not run.files.path("launched").exists()


def test_execute_preserves_path_for_unsandboxed_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path, unsandboxed=True, authorized_by="tbhb-dev/example-repo#1")
    account_path = str(tmp_path / "account-bin")
    monkeypatch.setenv("PATH", account_path)

    def execute(*_args: object, **_kwargs: object) -> int:
        assert os.environ["PATH"] == account_path
        return 7

    monkeypatch.setattr(host_execution, "_execute", execute)
    assert host_execution.execute(run, "task") == 7


@pytest.mark.parametrize(
    ("outcome", "rc"), [("success", 0), ("failure", 1), ("timeout", 124), ("exit", 7)]
)
def test_exec_removes_its_pytest_tree_after_each_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, rc: int
) -> None:
    run = make_run(tmp_path, timeout=1)
    other_tree = tmp_path / "pytest-of-another-run"
    other_tree.mkdir()
    other_file = other_tree / "test-file"
    other_file.write_text("keep")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    run_dirs: list[Path] = []
    test = tmp_path / "test_probe.py"
    test.write_text(
        "from pathlib import Path\n"
        "from os import environ\n"
        "def test_temp(tmp_path):\n"
        "    assert tmp_path.is_relative_to(Path(environ['TMPDIR']))\n"
        "    tmp_path.joinpath('test-file').write_text('example')\n"
    )

    def load(_args: object, directory: Path) -> tuple[state.Run, str]:
        run.tmpdir = runfiles.private_dir(directory / "tools")
        run_dirs.append(directory)
        return run, "example task"

    def prepare(*_args: object) -> state.Prepared:
        return preparation(run)

    def launch(spec: process_group.Launch, **kwargs: object) -> Child:
        assert (
            spec.env["TMPDIR"] == spec.env["PYTEST_DEBUG_TEMPROOT"] == str(run.tmpdir)
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--confcutdir",
                str(tmp_path),
                str(test),
            ],
            env={
                **os.environ,
                "TMPDIR": spec.env["TMPDIR"],
                "PYTEST_DEBUG_TEMPROOT": spec.env["PYTEST_DEBUG_TEMPROOT"],
                "PYTEST_ADDOPTS": "",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            check=True,
            capture_output=True,
        )
        assert next(run.tmpdir.glob("pytest-of-*/pytest-*/test_temp0/test-file"))
        (run.tmpdir / "outside").symlink_to(other_tree, target_is_directory=True)
        if outcome == "failure":
            raise OSError
        child = Child(code=rc, timeout=outcome == "timeout")
        cast("Callable[[Child], None]", kwargs["started"])(child)
        return child

    monkeypatch.setattr(host_intake, "files_for", constant(run.files))
    monkeypatch.setattr(host_intake, "load", load)
    monkeypatch.setattr(host_preflight, "preflight", constant(None))
    monkeypatch.setattr(process_group, "prepare", prepare)
    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_owned", constant(value=True))
    assert host_dispatcher.main(["example"], "run-host") == rc
    assert run.files.path("rc").read_text() == f"{rc}\n"
    assert len(run_dirs) == 1
    assert not run_dirs[0].exists()
    assert other_file.read_text() == "keep"


@pytest.mark.parametrize(
    ("hook_reference", "enabled", "expected"),
    [(True, True, 7), (True, False, 1), (False, False, 7)],
)
def test_run_builds_required_shims_before_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_reference: bool,
    enabled: bool,
    expected: int,
) -> None:
    run = make_run(tmp_path)
    run.host = dataclasses.replace(run.host, build_shims=enabled)
    prepared = dataclasses.replace(preparation(run), auth_before="example-before")
    workspace = Path(prepared.paths.worktree)
    if hook_reference:
        (workspace / "prek.toml").write_text(".bazel_env/bin/cog")
    runtime = HostRuntime(run, prepared, None)
    runtime.patch(monkeypatch)
    monkeypatch.setattr(host_preflight, "preflight", Mock())
    tool = Mock(return_value="/tools/bazelisk")
    monkeypatch.setattr(host_bazel_shutdown, "bazel_tool", tool)
    deadline = run.deadline
    ticks = iter([100, 130])
    monkeypatch.setattr(
        host_execution,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), time=time.time),
    )

    def build(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert runtime.phases == ["check", "workspace", "shims"]
        assert runtime.events == []
        cog = workspace / ".bazel_env/bin/cog"
        cog.parent.mkdir(parents=True)
        cog.touch()
        return subprocess.CompletedProcess([], 1, b"", b"")

    call = Mock(side_effect=build)
    monkeypatch.setattr(shims.subprocess, "run", call)
    assert host_execution.execute(run, "example") == expected
    assert run.files.path("rc").read_text() == f"{expected}\n"
    if enabled:
        assert runtime.events == ["group-stopped", "auth"]
        assert runtime.phases == ["check", "workspace", "shims", "ended"]
        assert run.deadline == deadline + 30
        assert tool.call_count == 2  # Shim build and Bazel shutdown.
        assert tool.call_args_list[0].args == (run,)
        call.assert_called_once()
    else:
        assert run.deadline == deadline
        tool.assert_called_once_with(run)  # Cleanup still looks for Bazel.
        call.assert_not_called()
        if hook_reference:
            assert runtime.events == ["auth"]
            assert runtime.phases == ["check", "workspace", "ended"]
            assert (
                "hook shims: the hooks need Bazel shims and this host has "
                "build_shims = false; run on another host\n"
                in run.files.path("log").read_text()
            )
        else:
            assert runtime.events == ["group-stopped", "auth"]
            assert runtime.phases == ["check", "workspace", "brief", "ended"]


def test_preflight_skips_build_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path, expect_commit=True)
    prepared = dataclasses.replace(preparation(run), auth_before="example-before")
    runtime = HostRuntime(run, prepared, None)
    runtime.patch(monkeypatch)
    (Path(prepared.paths.worktree) / "prek.toml").write_text(".bazel_env/bin/cog")

    def probe(
        actual: state.Run,
        ready: state.Prepared,
        stack: ExitStack,
        *,
        preflight_only: bool,
    ) -> int:
        assert actual is run
        assert ready is prepared
        assert isinstance(stack, ExitStack)
        assert preflight_only is True
        return 0

    build = Mock()
    monkeypatch.setattr(host_execution, "build_shims", build)
    monkeypatch.setattr(host_harness, "run_harness", probe)
    assert host_execution.execute(run, "example", preflight_only=True) == 0
    build.assert_not_called()


def test_codex_preflight_without_github_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            credentials=tuple(
                dataclasses.replace(c, gh=None) for c in run.settings.credentials
            ),
        ),
    )
    prepared = preparation(run)
    original_command = host_harness.harness_command
    commands: list[tuple[str, ...]] = []

    def command(
        value: state.Run, ready: state.Prepared, policy: Policy, executable: str
    ) -> tuple[str, ...]:
        result = original_command(value, ready, policy, executable)
        overrides = "\n".join(
            value for index, value in enumerate(result) if result[index - 1] == "-c"
        )
        env = tomllib.loads(overrides)["shell_environment_policy"]["set"]
        assert env["GH_CONFIG_DIR"] == str(run.tmpdir / "gh")
        assert env["GH_CONFIG_DIR"] == f"{env['TMPDIR']}/gh"
        commands.append(result)
        return result

    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(host_harness, "harness_command", command)
    assert host_execution.execute(run, "example", preflight_only=True) == 0
    assert len(commands) == 1
    assert f"preflight ok: {commands[0][0]}\n" in run.files.path("log").read_text()
    assert run.files.path("rc").read_text() == "0\n"
    assert not run.files.path("launched").exists()


@pytest.mark.parametrize("harness", Harness)
def test_the_brief_carries_the_harness_subagent_rule(
    tmp_path: Path, harness: Harness
) -> None:
    """Agy and Codex, whose controls are prompt-only gaps, get a rule in the brief."""
    run = make_run(
        tmp_path,
        harness=harness,
        timeout=60 if harness is Harness.AGY else None,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example reason" if harness is Harness.CLAUDE else None,
    )
    host_execution.prepare_brief(run, tmp_path, "example task", preflight_only=True)
    text = run.files.path("prompt").read_text()
    assert (render.AGY_SUBAGENTS in text) is (harness is Harness.AGY)
    assert (render.CODEX_SUBAGENTS in text) is (harness is Harness.CODEX)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (None, 7),
        ("timeout", 124),
        ("interrupt", 143),
        ("invalid", 1),
        ("os", 1),
        ("auth", 1),
    ],
)
def test_finish_orders_cleanup_auth_agent_index_and_atomic_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None, code: int
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    prepared = dataclasses.replace(
        prepared,
        auth_before="example-before",
        delivery=credentials.Delivery({}, "example-key", claude_profile=None),
    )
    runtime = HostRuntime(run, prepared, failure)
    runtime.patch(monkeypatch)
    assert host_execution.execute(run, "example") == code
    assert run.files.path("rc").read_text() == f"{code}\n"
    assert run.files.path("phase").read_text() == "ended"
    assert run.files.path("request.json").stat().st_mode & 0o777 == 0o600
    assert "example-key" not in run.files.path("request.json").read_text()
    if failure in (None, "auth"):
        assert runtime.events == [
            "agent-started",
            "group-stopped",
            "auth",
            "agent-stopped",
        ]
        line = runfiles.latest(run.files.root)["example"]
        assert line["worktree"] == prepared.paths.worktree
        assert line["state"] == "ended"
        assert line["rc"] == code
        assert line["trace_id"] is None
        assert line["v"] == 2
    else:
        assert not run.files.path("launched").exists()
    assert runtime.phases == (
        ["check", "workspace", "brief", "ended"]
        if failure in (None, "auth")
        else ["check", "ended"]
    )
    messages = {
        "invalid": "safe preparation error\n",
        "os": "run failed; check the clone and credential references\n",
        "auth": "codex auth write-back failed\n",
    }
    notice = "unsandboxed full access authorized_by=None\n"
    assert run.files.path("log").read_text() == notice + messages.get(failure or "", "")
    if failure in (None, "auth"):
        assert run.files.path("prompt").read_text() == render.brief(
            "example",
            profile=run.request.profile,
            harness=run.request.harness,
        )
        entries = [
            json.loads(line)
            for line in (run.files.root / runfiles.INDEX).read_text().splitlines()
        ]
        assert entries[0]["worktree"] == prepared.paths.worktree
        assert entries[0]["state"] == "running"
        assert entries[0]["rc"] is None
