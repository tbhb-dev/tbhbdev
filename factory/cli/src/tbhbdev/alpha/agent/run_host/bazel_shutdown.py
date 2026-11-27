"""Stop Bazel servers that belong to the run or its output bases."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import bazel_servers
from tbhbdev.alpha.agent.run_host import bazel_cache, environment

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import BazelShutdown, Prepared, Run

_BAZEL_SHUTDOWN_SECONDS = 60


_BAZEL_SHUTDOWN_RETRIES = 3


_BAZEL_RETRY_SECONDS = 2


def bazel_tool(run: Run) -> str | None:
    # The harness ran with the configured tool path, so search it as well.
    search = ":".join(
        (
            os.environ.get("PATH", os.defpath),
            environment.tool_path(run.settings, run.host, str(run.home)),
        )
    )
    return shutil.which("bazelisk", path=search) or shutil.which("bazel", path=search)


def _shutdown_once(tool: str, base: Path, worktree: str) -> str:
    try:
        done = subprocess.run(  # nosec B603
            [tool, f"--output_base={base}", "shutdown"],
            cwd=worktree,
            capture_output=True,
            timeout=_BAZEL_SHUTDOWN_SECONDS,
            check=False,
        )
        result = f"rc={done.returncode}"
    except subprocess.TimeoutExpired:
        result = "timed out"
    except OSError:
        result = "failed to run"
    if result == "rc=0" and bazel_cache.server_alive(base):
        result = "server alive"
    return result


def _shutdown_base(tool: str, base: Path, worktree: str) -> str:
    # A server still starting can answer shutdown with rc 0 before it accepts
    # commands, so a live server after a clean shutdown gets another attempt.
    result = _shutdown_once(tool, base, worktree)
    for _ in range(_BAZEL_SHUTDOWN_RETRIES):
        if result != "server alive":
            break
        time.sleep(_BAZEL_RETRY_SECONDS)
        result = _shutdown_once(tool, base, worktree)
    return result


def _recorded_bases(
    run: Run, prepared: Prepared
) -> dict[Path, tuple[Path, tuple[int, int]]]:
    recorded: dict[Path, tuple[Path, tuple[int, int]]] = {}
    for base in prepared.bases:
        status = bazel_cache.owned_base(run.home, base)
        if status is None:
            if os.path.lexists(base):
                run.files.write("log", f"bazel cleanup: refused {base}\n")
            continue
        recorded[Path(os.path.realpath(base))] = (base, (status.st_dev, status.st_ino))
    return recorded


def _shutdown_target(
    tool: str | None, base: Path, worktree: Path, server: bazel_servers.Server | None
) -> str:
    if tool is None:
        return "tool unavailable"
    if server is not None:
        if not bazel_servers.matches_pid(base, server.pid):
            return "server changed"
        if server.workspace.is_dir():
            worktree = server.workspace
    return _shutdown_base(tool, base, str(worktree))


def shutdown_bazel(
    run: Run,
    prepared: Prepared | None,
    *,
    group: int | None = None,
    servers: dict[Path, bazel_servers.Server] | None = None,
) -> BazelShutdown:
    """Stop the run's Bazel servers and mark recorded bases for deletion."""
    if prepared is None:
        return {}
    tool = bazel_tool(run)
    worktree = Path(prepared.paths.worktree)
    recorded = _recorded_bases(run, prepared)
    targets: dict[Path, bazel_servers.Server | None] = dict.fromkeys(recorded)
    if servers is None and group is not None:
        servers = bazel_servers.find(worktree, group)
    targets.update(servers or {})
    stopped: BazelShutdown = {}
    for base, server in targets.items():
        if server is None and tool is None:
            continue
        if server is not None and not server.belongs:
            continue
        path, identity = recorded.get(base, (base, None))
        result = _shutdown_target(tool, path, worktree, server)
        run.files.write("log", f"bazel shutdown: {str(path)!r} ({result})\n")
        stopped[path] = (result, identity)
    return stopped
