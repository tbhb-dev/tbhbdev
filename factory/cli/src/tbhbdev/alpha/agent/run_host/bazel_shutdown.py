"""Stop Bazel servers that belong to the run or its output bases."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import bazel_servers
from tbhbdev.alpha.agent.run_host import bazel_cache

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import BazelShutdown, Prepared, Run


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


def shutdown_bazel(
    run: Run,
    prepared: Prepared | None,
    *,
    servers: dict[Path, bazel_servers.Server] | None = None,
) -> BazelShutdown:
    """Stop the run's Bazel servers and mark recorded bases for deletion."""
    if prepared is None:
        return {}
    worktree = Path(prepared.paths.worktree)
    recorded = _recorded_bases(run, prepared)
    targets: dict[Path, bazel_servers.Server | None] = dict.fromkeys(recorded)
    previous = servers or {}
    targets.update(previous)
    # Rescan after stopping the harness to catch servers launched during teardown.
    targets.update(
        (base, server)
        for base, server in bazel_servers.find(worktree).items()
        if base not in previous or server.belongs
    )
    stopped: BazelShutdown = {}
    for base, server in targets.items():
        if server is not None and not server.belongs:
            continue
        path, identity = recorded.get(base, (base, None))
        if (
            identity is None
            and (status := bazel_cache.owned_base(run.home, path)) is not None
        ):
            identity = (status.st_dev, status.st_ino)
        result = bazel_servers.stop(path, server) if server is not None else "rc=0"
        run.files.write("log", f"bazel shutdown: {str(path)!r} ({result})\n")
        stopped[path] = (result, identity)
    return stopped
