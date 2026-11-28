"""Coordinate the stages of a host run and its completion."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import time
from contextlib import ExitStack
from pathlib import Path

from tbhbdev.alpha.agent import (
    admission,
    credentials,
    host_tools,
    process_group,
    push,
    render,
    shims,
)
from tbhbdev.alpha.agent.model import Invalid, Profile
from tbhbdev.alpha.agent.run_host import (
    completion,
    environment,
    harness,
    preflight,
    publication,
    sweep_first,
    workspace,
)
from tbhbdev.alpha.agent.run_host.state import Prepared, Run, RunInterruptedError


def execute(run: Run, text: str, *, preflight_only: bool = False) -> int:
    """Keep cleanup protected until the completion record is durable."""
    with host_tools.configured(run.settings, run.host.os), ExitStack() as stack:
        if not preflight_only:
            slot = (
                admission.ROOT,
                run.host,
                run.request.profile,
                run.request.max_concurrent_runs,
            )
            # A launch the slots refuse pays nothing: reserve refuses it below.
            if admission.has_room(*slot):
                sweep_first.run_sweep(run)
            stack.enter_context(admission.reserve(*slot))
        return _execute(run, text, stack, preflight_only=preflight_only)


def _execute(
    run: Run,
    text: str,
    stack: ExitStack,
    *,
    preflight_only: bool,
) -> int:
    prepared = None
    rc = 1
    agent = None
    try:
        run.files.path("head").unlink(missing_ok=True)
        if not run.files.path("request.json").exists():
            run.files.receive(run.request)
        run.files.write("phase", "check")
        preflight.preflight(run)
        prepared = process_group.prepare(
            lambda: workspace.prepare_workspace(run),
            run.files,
            run.deadline,
            run.settings.kill_grace_seconds,
        )
        completion.record_index(run, prepared, None)
        run.files.write("phase", "workspace")
        worktree = Path(prepared.paths.worktree)
        prepare_brief(run, worktree, text, preflight_only=preflight_only)
        if prepared.delivery.signing is not None:
            agent = credentials.signing_agent(
                prepared.delivery.signing, run.tmpdir, run.deadline
            )
        if preflight_only:
            rc = harness.run_harness(run, prepared, stack, preflight_only=True)
        else:
            rc = run_and_check(run, prepared, stack)
    except TimeoutError, subprocess.TimeoutExpired:
        rc = 124
    except RunInterruptedError, EOFError:
        rc = 143
    except Invalid as exc:
        run.files.write("log", f"{exc}\n")
    except OSError, ValueError, subprocess.SubprocessError:
        run.files.write(
            "log", "run failed; check the clone and credential references\n"
        )
    finally:
        stack.enter_context(process_group.ignore_signals())
        rc = completion.finish(run, prepared, agent, rc)
    return rc


def run_and_check(run: Run, prepared: Prepared, stack: ExitStack) -> int:
    rc = push.run(run, prepared, stack, harness.run_harness)
    if rc == 0 and run.request.profile is Profile.REVIEW:
        return publication.post_review(run, prepared)
    return publication.require_commit(run, rc)


def prepare_brief(
    run: Run, workspace: Path, text: str, *, preflight_only: bool
) -> None:
    needs_shims = not preflight_only and shims.needed(workspace, run.request.profile)
    if needs_shims:
        build_shims(run, workspace)
    run.files.write(
        "prompt",
        render.brief(
            text,
            profile=run.request.profile,
            harness=run.request.harness,
            start_ref=run.request.start_ref,
        ),
    )
    if not needs_shims:
        run.files.write("phase", "brief")


def build_shims(run: Run, workspace: Path) -> None:
    if not run.host.build_shims:
        message = (
            "hook shims: the hooks need Bazel shims and this host has "
            "build_shims = false; run on another host"
        )
        raise Invalid(message)
    # Keep this phase until launch so polling uses the extra time through startup.
    run.files.write("phase", "shims")
    started = time.monotonic()
    search = ":".join(
        (
            os.environ.get("PATH", os.defpath),
            environment.tool_path(run.settings, run.host, str(run.home)),
        )
    )
    tool = shutil.which("bazelisk", path=search) or shutil.which("bazel", path=search)
    shims.build(workspace, tool)
    run.deadline += time.monotonic() - started
