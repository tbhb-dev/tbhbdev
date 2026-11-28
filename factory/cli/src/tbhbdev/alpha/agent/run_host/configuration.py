"""Check the audit destination against protected account paths."""

from __future__ import annotations

import os
from pathlib import Path

from tbhbdev import userconfig
from tbhbdev.alpha.agent.run_host.account_paths import (
    protect_paths,
    protected_home_paths,
)
lazy from tbhbdev.alpha.agent.model import Settings


def check_config(configured: Settings) -> None:
    """Check configured writes against host trust paths before a run exists."""
    home = Path.home()
    writes = (configured.sandbox.audit_dir,)
    protected = (
        *protected_home_paths(home, configured.sockets_dir),
        userconfig.path(),
    )
    if os.environ.get("ZDOTDIR"):
        protected = (*protected, home / os.environ["ZDOTDIR"])
    protect_paths(
        tuple(str((home / p).resolve()) for p in writes),
        tuple(
            dict.fromkeys(str(alias) for p in protected for alias in (p, p.resolve()))
        ),
    )
