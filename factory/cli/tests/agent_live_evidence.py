"""Refresh a disposable Codex auth copy using the run-host write-back path."""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import credentials, runfiles
from tbhbdev.alpha.agent.model import Harness
from tbhbdev.alpha.agent.run_host.auth import finish_auth
from tbhbdev.alpha.agent.run_host.state import Prepared, Run

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import RunPaths


@contextmanager
def codex_auth(
    run: Run, paths: RunPaths, account_home: Path, root: Path
) -> Generator[RunPaths]:
    assert run.request.harness is Harness.CODEX
    directory = Path(paths.codex_home)
    assert directory.resolve().is_relative_to(root.resolve())
    runfiles.private_dir(directory)
    before = None
    try:
        before = credentials.auth_copy(
            account_home / ".codex" / "auth.json", directory / "auth.json"
        )
        yield paths
    finally:
        prepared = Prepared(
            paths,
            credentials.Delivery({}, None, claude_profile=None),
            before,
        )
        reason = finish_auth(dataclasses.replace(run, home=account_home), prepared)
        assert reason is None, reason
        assert not list(root.rglob("auth.json"))
