"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
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

from tbhbdev import telemetry
from tbhbdev.alpha.agent import (
    credentials,
    process_group,
    push,
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
from tbhbdev.alpha.agent.run_host import publication as host_publication
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


NOT_REPORTED = "not reported"
CODEX_LOG = "answer\ntokens used\n54,534\nbazel shutdown: 'x' (rc=0)\n"


def end_row(run: state.Run) -> dict[str, object]:
    return runfiles.latest(run.files.root)[run.files.name]


def test_finished_run_index_row_has_usage_phases_and_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    run.files.write("log", CODEX_LOG)
    run.files.write("branch", "agent/example")
    run.files.write("pr", "example-org/example-repo#12")
    run.setup_seconds, run.model_seconds, run.publication_seconds = 1.5, 20.0, 0.25
    monkeypatch.setattr(telemetry, "attributes", sent := Mock())
    assert host_completion.finish(run, prepared, None, 0) == 0
    row = end_row(run)
    assert row["usage"] == {
        "input_tokens": NOT_REPORTED,
        "output_tokens": NOT_REPORTED,
        "total_tokens": 54534,
        "cost_usd": NOT_REPORTED,
        "source": "codex log",
    }
    assert row["phase_seconds"] == {"setup": 1.5, "model": 20.0, "publication": 0.25}
    assert row["published"] == {"branch": "agent/example", "pr": 12}
    attributes = sent.call_args.kwargs
    assert attributes["tbhb.run.usage.total_tokens"] == 54534
    assert attributes["tbhb.run.phase.model_seconds"] == 20.0
    assert attributes["tbhb.run.published.pr"] == 12


def test_run_without_branch_or_pr_files_published_nothing(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.files.write("pr", "garbage")
    assert host_completion.finish(run, None, None, 0) == 0
    assert end_row(run)["published"] == {"branch": None, "pr": None}


def test_run_that_raised_before_launch_has_no_model_or_publication_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)

    def refuse(_run: state.Run) -> None:
        message = "preflight refused"
        raise Invalid(message)

    monkeypatch.setattr(host_preflight, "preflight", refuse)
    assert host_execution.execute(run, "example") == 1
    row = end_row(run)
    phases = cast("dict[str, float | None]", row["phase_seconds"])
    assert phases["model"] is None
    assert phases["publication"] is None
    assert set(row["usage"].values()) == {NOT_REPORTED}  # type: ignore[union-attr]


def test_unreadable_log_keeps_the_rc_and_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    run.files.write("log", CODEX_LOG)
    real_open = Path.open

    def opening(path: Path, *args: object, **kwargs: object) -> object:
        if path == run.files.path("log") and args[:1] == ("rb",):
            raise PermissionError
        return real_open(path, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", opening)
    assert host_completion.finish(run, None, None, 7) == 7
    assert run.files.path("rc").read_text() == "7\n"
    row = end_row(run)
    assert row["rc"] == 7
    assert row["usage"]["total_tokens"] == NOT_REPORTED  # type: ignore[index]


def test_publication_seconds_run_from_the_end_of_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    run.waited = 10.0
    monkeypatch.setattr(host_execution, "time", SimpleNamespace(monotonic=lambda: 14.5))
    monkeypatch.setattr(push, "run", constant(0))
    monkeypatch.setattr(host_publication, "require_commit", constant(0))
    assert host_execution.run_and_check(run, preparation(run), ExitStack()) == 0
    assert run.publication_seconds == 4.5
