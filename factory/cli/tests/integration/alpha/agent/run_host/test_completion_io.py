"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import cast
from unittest.mock import Mock
lazy import subprocess

import pytest
from agent_host_example import (
    constant,
    make_run,
    repository,
)
from run_host_fixtures import (
    HOST_FIXTURES,
    Child,
    bazel_run,
    make_base,
    preparation,
)

from tbhbdev.alpha.agent import (
    credentials,
    process_group,
    rescue,
    runfiles,
)
from tbhbdev.alpha.agent.model import (
    Invalid,
)
from tbhbdev.alpha.agent.run_host import auth as host_auth
from tbhbdev.alpha.agent.run_host import bazel_shutdown as host_bazel_shutdown
from tbhbdev.alpha.agent.run_host import completion as host_completion
from tbhbdev.alpha.agent.run_host import execution as host_execution
from tbhbdev.alpha.agent.run_host import harness as host_harness
from tbhbdev.alpha.agent.run_host import preflight as host_preflight
from tbhbdev.alpha.agent.run_host import state

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


@pytest.mark.parametrize("code", [0, 7], ids=["finished", "failed"])
def test_finished_run_writes_host_audit_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    _, workspace = repository(tmp_path / "repository")

    def harness(*_args: object) -> int:
        audit = Path(prepared.paths.audit_dir)
        (audit / "nested").mkdir(parents=True)
        (audit / "nested" / "event.json").write_bytes(b"event\n")
        (audit / "empty").write_bytes(b"")
        (audit / "link").symlink_to(workspace / "tracked")
        return code

    monkeypatch.setattr(host_preflight, "preflight", constant(None))
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(host_harness, "run_harness", harness)
    assert host_execution.execute(run, "example") == code
    summary = run.files.path("audit.json")
    assert summary.parent == run.files.path("rc").parent
    assert summary.stat().st_mode & 0o777 == 0o600
    assert json.loads(summary.read_text()) == {
        "rc": code,
        "files": {
            "empty": hashlib.sha256(b"").hexdigest(),
            "nested/event.json": hashlib.sha256(b"event\n").hexdigest(),
        },
    }


def test_failed_preparation_still_writes_host_audit_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)

    def refuse(_run: state.Run) -> None:
        message = "preflight refused"
        raise Invalid(message)

    monkeypatch.setattr(host_preflight, "preflight", refuse)
    assert host_execution.execute(run, "example") == 1
    assert json.loads(run.files.path("audit.json").read_text()) == {
        "rc": 1,
        "files": {},
    }


def test_audit_summary_error_preserves_the_runs_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    monkeypatch.setattr(host_completion, "audit_summary", Mock(side_effect=OSError))
    assert host_completion.finish(run, None, None, 7) == 7
    assert run.files.path("rc").read_text() == "7\n"
    assert run.files.path("log").read_text() == "audit summary write failed\n"


def index_should_fail(failure: str, entry: dict[str, object]) -> bool:
    return (failure == "initial-index" and entry["rc"] is None) or (
        failure == "final-index" and entry["rc"] is not None
    )


@pytest.mark.parametrize("failure", ["initial-index", "final-index", "agent", "phase"])
def test_cleanup_failures_cannot_skip_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    run = make_run(tmp_path)
    prepared = dataclasses.replace(
        preparation(run),
        delivery=credentials.Delivery({}, "key", claude_profile=None),
    )
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(host_harness, "run_harness", constant(7))
    monkeypatch.setattr(credentials, "signing_agent", constant(Child()))
    stopped: list[str] = []

    def stop(_child: object) -> None:
        stopped.append("stopped")
        if failure == "agent":
            raise OSError

    append = runfiles.append

    def index(root: Path, entry: dict[str, object]) -> None:
        if index_should_fail(failure, entry):
            if entry["rc"] is not None:
                assert run.files.path("rc").read_text() == "7\n"
            raise OSError
        append(root, entry)

    write = run.files.write

    def record(suffix: str, text: str) -> None:
        if failure == "phase" and text == "ended":
            raise OSError
        write(suffix, text)

    monkeypatch.setattr(host_auth, "stop_agent", stop)
    monkeypatch.setattr(runfiles, "append", index)
    monkeypatch.setattr(run.files, "write", record)
    assert host_execution.execute(run, "example") == (
        1 if failure == "initial-index" else 7
    )
    check_cleanup_result(run, stopped, failure)


def check_cleanup_result(run: state.Run, stopped: list[str], failure: str) -> None:
    assert run.files.path("rc").read_text() == (
        "1\n" if failure == "initial-index" else "7\n"
    )
    assert stopped == ([] if failure == "initial-index" else ["stopped"])
    if failure == "final-index":
        assert (
            run.files.path("log")
            .read_text()
            .endswith("completion index append failed\n")
        )


def test_signer_stop_denial_keeps_harness_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    child = Child()
    monkeypatch.setattr(process_group, "stop_started", constant(value=False))
    agent = cast("subprocess.Popen[str]", child)
    assert host_auth.stop_agent(agent) is False
    assert child.waits == []
    assert host_completion.finish(run, None, agent, 7) == 7
    assert run.files.uncertain is True
    assert run.files.path("phase").read_text() == "unknown"
    assert run.files.path("rc").read_text() == "7\n"


def test_finish_survives_a_locked_base_and_a_failing_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    (base / "dead").mkdir()
    (base / "dead").chmod(0)
    entries: list[dict[str, object]] = []

    def record(_root: Path, entry: dict[str, object]) -> None:
        entries.append(entry)

    monkeypatch.setattr(runfiles, "append", record)
    assert host_completion.finish(run, prepared, None, 0) == 0
    assert not base.exists()
    assert run.files.path("rc").read_text() == "0\n"
    assert entries[0]["bazel_shutdown"] == "rc=0"
    assert entries[0]["bazel_cleanup"]["bases"] == 1  # type: ignore[index]


@pytest.mark.parametrize("rc", [0, 3, 7])
def test_finish_records_rc_phase_and_index_when_cleanup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    run = make_run(tmp_path)
    monkeypatch.setattr(
        host_bazel_shutdown, "shutdown_bazel", Mock(side_effect=TypeError)
    )
    if rc == 3:
        monkeypatch.setattr(rescue, "on_fault", Mock(side_effect=RuntimeError))
    assert host_completion.finish(run, None, None, rc) == rc
    assert run.files.path("phase").read_text().strip() == "ended"
    entry = runfiles.latest(run.files.root)["example"]
    assert (entry["bazel_shutdown"], entry["bazel_cleanup"]) == ("skipped", {})
    assert entry["rc"] == rc
    assert run.files.path("audit.json").exists()
    assert ("fault publication failed" in run.files.path("log").read_text()) == (
        rc == 3
    )
