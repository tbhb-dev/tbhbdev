"""Write the final outcome after bounded host cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING
lazy import subprocess  # nosec B404

from tbhbdev import telemetry
from tbhbdev.alpha.agent import process_group, rescue, runfiles
from tbhbdev.alpha.agent.model import Harness, Invalid
from tbhbdev.alpha.agent.run_host import (
    auth,
    bazel_cache,
    bazel_shutdown,
    run_usage,
)
from tbhbdev.alpha.agent.run_host.index import IndexRun, index_entry
from tbhbdev.alpha.agent.run_host.paths import planned_paths
from tbhbdev.alpha.agent.run_host.telemetry import run_end_attributes

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import Prepared, Run

OUTPUT_LIMIT = 4 * 1024 * 1024


def finish(
    run: Run, prepared: Prepared | None, agent: subprocess.Popen[str] | None, rc: int
) -> int:
    bazel = ""
    cleanup: dict[str, int] = {}
    try:
        try:
            reason = auth.finish_auth(run, prepared)
            if reason is not None:
                rc = 1
                run.files.write("log", reason)
        except OSError, ValueError:
            rc = 1
            run.files.write("log", "codex auth write-back failed\n")
    finally:
        try:
            if agent is not None and auth.stop_agent(agent) is False:
                process_group.unknown(run.files)
        except OSError, Invalid:
            process_group.unknown(run.files)
        finally:
            try:
                stopped = run.bazel_shutdown
                if stopped is None:
                    stopped = bazel_shutdown.shutdown_bazel(run, prepared)
                bazel = ",".join(sorted({result for result, _ in stopped.values()}))
                cleanup = bazel_cache.cleanup_bazel(run, prepared, stopped)
            except Exception:  # noqa: BLE001 -- completion survives cleanup failures
                # Nothing from the Bazel cleanup may block the rc and completion record.
                with suppress(Exception):
                    run.files.write("log", "bazel cleanup failed\n")
            try:
                run.files.write("phase", "unknown" if run.files.uncertain else "ended")
            except OSError:
                process_group.unknown(run.files)
            finally:
                run.files.write("rc", f"{rc}\n")
    with suppress(Exception), ExitStack() as publication:
        publication.callback(run.files.write, "log", "fault publication failed\n")
        rescue.on_fault(run, prepared, rc)
        publication.pop_all()
    try:
        audit_summary(run, prepared, rc)
    except OSError, ValueError:
        run.files.write("log", "audit summary write failed\n")
    outcome = run_outcome(run)
    telemetry.attributes(
        **run_end_attributes(
            outcome["usage"], outcome["phase_seconds"], outcome["published"]
        )
    )
    try:
        record_index(
            run,
            prepared,
            rc,
            bazel_shutdown=bazel or "skipped",
            bazel_cleanup=cleanup,
            **outcome,
        )
    except OSError, ValueError:
        run.files.write("log", "completion index append failed\n")
    return rc


def audit_summary(run: Run, prepared: Prepared | None, rc: int) -> None:
    paths = planned_paths(run) if prepared is None else prepared.paths
    audit = Path(paths.audit_dir)
    files: dict[str, str] = {}
    for path in sorted(audit.rglob("*")):
        if not path.is_symlink() and path.is_file():
            with path.open("rb") as stream:
                files[path.relative_to(audit).as_posix()] = hashlib.file_digest(
                    stream, hashlib.sha256
                ).hexdigest()
    run.files.write(
        "audit.json",
        json.dumps({"rc": rc, "files": files}) + "\n",
    )


def record_index(
    run: Run, prepared: Prepared | None, rc: int | None, **details: object
) -> None:
    facts = IndexRun(
        run.request,
        run.files.name,
        run.host.name,
        run.started,
        prepared.paths.worktree if prepared is not None else None,
        prepared.delivery.claude_profile
        if prepared is not None
        else run.request.claude_profile,
    )
    ended = time.time() if rc is not None else None
    runfiles.append(run.files.root, index_entry(facts, rc, ended=ended) | details)


def run_outcome(run: Run) -> dict[str, dict[str, object]]:
    """The end record's usage, phase seconds and published state; never raises."""
    phases: dict[str, object] = {
        "setup": run.setup_seconds,
        "model": run.model_seconds,
        "publication": run.publication_seconds,
    }
    if phases["setup"] is None:
        # The run raised in setup: that is how long it ran before it stopped.
        phases["setup"] = time.monotonic() - run.begun
    usage: dict[str, object] = dict(run_usage.unreported())
    with suppress(Exception):
        usage = dict(
            run_usage.parse(
                run.request.harness,
                _read_tail(run.files.path("log"), run_usage.LOG_TAIL_BYTES),
                _read_tail(run.files.path("out"), OUTPUT_LIMIT)
                if run.request.harness is not Harness.CODEX
                else "",
            )
        )
    published: dict[str, object] = {"branch": None, "pr": None}
    with suppress(Exception):
        published = {
            "branch": _read_tail(run.files.path("branch"), 256).strip() or None,
            "pr": _pr_number(_read_tail(run.files.path("pr"), 256)),
        }
    return {"usage": usage, "phase_seconds": phases, "published": published}


def _read_tail(path: Path, limit: int) -> str:
    """The last LIMIT bytes of PATH, or "" when it cannot be read."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read(limit).decode(errors="replace")
    except OSError:
        return ""


def _pr_number(text: str) -> int | None:
    """The number of a `pr` run file: `owner/repo#12`, or a bare `12`."""
    _, _, number = text.strip().rpartition("#")
    return int(number) if number.isascii() and number.isdigit() else None
