"""Shared test inputs and clocks for coordinator I/O tests."""

from __future__ import annotations

import itertools
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import TYPE_CHECKING

from agent_cli_example import run_agent
from agent_driver_example import execution_arguments
from agent_example import MARKER, settings

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.driver import hosts, lifecycle, metadata, startup, version_heal
from tbhbdev.alpha.agent.driver import launch as coordinator

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from agent_driver_example import Remote


CONFIG = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")


COMMIT = "0123456789abcdef0123456789abcdef01234567"


PREPARE = coordinator.prepare


def arguments(remote: Remote, *options: str) -> list[str]:
    return execution_arguments(remote.prompt, *options)


def launch(remote: Remote, *options: str) -> int:
    return run_agent(arguments(remote, *options))


def command(verb: str, *options: str) -> int:
    return run_agent([verb, "sample", *options])


def kept() -> str:
    tree = repo.clone_path(
        PurePosixPath("."), metadata.load(CONFIG, "sample").request.repo
    )
    return f"sample: worktree kept at example-linux:~/{tree}/.worktrees/agent-sample\n"


def set_clock(
    monkeypatch: pytest.MonkeyPatch,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None] = lambda _: None,
) -> None:
    clock = SimpleNamespace(monotonic=monotonic, sleep=sleep)
    for module in (coordinator, hosts, lifecycle, startup, version_heal):
        monkeypatch.setattr(module, "time", clock)


def ticking(monkeypatch: pytest.MonkeyPatch, step: float = 100) -> None:
    ticks = itertools.count()
    set_clock(monkeypatch, lambda: next(ticks) * step)
