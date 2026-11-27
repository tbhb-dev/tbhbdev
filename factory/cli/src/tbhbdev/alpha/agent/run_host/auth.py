"""Recover refreshed Codex credentials and stop the signing agent."""

from __future__ import annotations

import shutil
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
lazy import subprocess  # nosec B404

from tbhbdev.alpha.agent import credentials, process_group, runfiles
from tbhbdev.alpha.agent.model import Harness
from tbhbdev.alpha.agent.run_host.paths import planned_paths

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import Prepared, Run


def recover_auth(run: Run, destination: Path) -> Path:
    parent = runfiles.private_dir(run.home / ".codex-recovery")
    instant = datetime.now(UTC)
    while True:
        stamp = instant.strftime("%Y%m%dT%H%M%S.%fZ")
        directory = parent / f"{run.files.name}-{stamp}"
        try:
            directory.mkdir(mode=0o700)
            break
        except FileExistsError:
            instant += timedelta(microseconds=1)
    recovered = directory / "auth.json"
    destination.replace(recovered)
    return recovered


def finish_auth(run: Run, prepared: Prepared | None) -> str | None:
    if run.request.harness is not Harness.CODEX:
        return None
    paths = planned_paths(run) if prepared is None else prepared.paths
    reason = None
    if prepared is not None and not run.files.uncertain:
        destination = Path(paths.codex_home) / "auth.json"
        reason = credentials.auth_write_back(
            run.home / ".codex" / "auth.json", destination, prepared.auth_before
        )
        if reason is not None:
            recovered = recover_auth(run, destination)
            reason = f"{reason.rstrip()}; recovery: {recovered}\n"
    with suppress(FileNotFoundError):
        shutil.rmtree(paths.codex_home)
    return reason


def stop_agent(agent: subprocess.Popen[str]) -> bool:
    stopped = process_group.stop_started(agent.pid, 0, agent.poll) is not False
    if stopped:
        agent.wait()
    return stopped
