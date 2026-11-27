"""Resolve the installed harness and check launch prerequisites."""

from __future__ import annotations

import dataclasses
import os
import sys

from tbhbdev.alpha.agent import credentials, host_tools
from tbhbdev.alpha.agent.model import Policy, Profile, RunPaths
from tbhbdev.alpha.agent.run_host import environment
from tbhbdev.alpha.agent.run_host.paths import planned_paths
lazy from tbhbdev.alpha.agent.run_host.state import Run


def preflight(run: Run) -> None:
    announce_access(run)
    resolve_harness(run, planned_paths(run))
    if run.request.profile is Profile.REVIEW:
        references = next(
            c for c in run.settings.credentials if c.account == run.request.account
        )
        credentials.require_reviews_app(references)


def announce_access(run: Run) -> None:
    notice = f"unsandboxed full access authorized_by={run.request.authorized_by}"
    print(notice, file=sys.stderr)
    run.files.write("log", notice + "\n")


def resolve_harness(run: Run, paths: RunPaths) -> tuple[Policy, str]:
    tool_search = ":".join(host_tools.account_path(paths.home))
    value = environment.build(run.settings, run.host, run.request, paths, tool_search)
    search_path = ":".join((os.environ.get("PATH", os.defpath), value.tool_path))
    executable = host_tools.resolve(
        run.request.harness.value, search_path, follow_symlinks=False
    )
    value = dataclasses.replace(value, env=value.env | {"PATH": search_path})
    return value, executable
