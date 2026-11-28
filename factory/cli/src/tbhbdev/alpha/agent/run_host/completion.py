"""Write the final outcome after bounded host cleanup."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING
lazy import subprocess  # nosec B404

from tbhbdev.alpha.agent import process_group, rescue, runfiles
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.agent.run_host import auth, bazel_cache, bazel_shutdown
from tbhbdev.alpha.agent.run_host.index import IndexRun, index_entry
from tbhbdev.alpha.agent.run_host.paths import planned_paths

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import Prepared, Run


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
    try:
        record_index(
            run, prepared, rc, bazel_shutdown=bazel or "skipped", bazel_cleanup=cleanup
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
