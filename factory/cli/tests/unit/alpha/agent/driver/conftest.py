"""Provide a fake run host for the coordinator I/O tests."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import setup
from driver_scenarios import CONFIG, PREPARE, ticking

from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.driver import launch as coordinator

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

    from agent_driver_example import Remote

    from tbhbdev.alpha.agent import model
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run


@pytest.fixture
def remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> Remote:
    assert isolated_home.is_dir()
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_settings, "load", lambda: CONFIG)

    # Keep coverage of management for recorded runs from before the new default.
    def legacy_prepare(
        config: model.Settings,
        args: argparse.Namespace,
        *,
        host: model.Host | None = None,
    ) -> Run:
        run = PREPARE(config, args, host=host)
        run.request = dataclasses.replace(
            run.request,
            unsandboxed=args.unsandboxed is not None,
        )
        return run

    monkeypatch.setattr(coordinator, "prepare", legacy_prepare)

    ticking(monkeypatch, 10)
    return result
