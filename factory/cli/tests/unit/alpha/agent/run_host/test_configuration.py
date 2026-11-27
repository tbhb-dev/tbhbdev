"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
lazy from pathlib import Path

import pytest
from agent_host_example import (
    host_settings,
)

from tbhbdev.alpha.agent.model import (
    Invalid,
)
from tbhbdev.alpha.agent.run_host import configuration as host_configuration


@pytest.mark.parametrize(
    ("write", "startup"),
    [
        (".cache", None),
        (".cache", ".example-startup"),
        (".example-startup/cache", ".example-startup"),
    ],
)
def test_config_check_protects_the_configured_startup_directory(
    monkeypatch: pytest.MonkeyPatch, write: str, startup: str | None
) -> None:
    if startup is None:
        monkeypatch.delenv("ZDOTDIR", raising=False)
    else:
        monkeypatch.setenv("ZDOTDIR", startup)
    original = host_settings()
    configured = dataclasses.replace(
        original, sandbox=dataclasses.replace(original.sandbox, audit_dir=write)
    )
    if write == ".cache":
        host_configuration.check_config(configured)
    else:
        with pytest.raises(
            Invalid, match=r"protected host path .* overlaps write root"
        ):
            host_configuration.check_config(configured)


@pytest.mark.parametrize(
    "entry",
    [
        ".zshenv",
        ".zshrc",
        ".zprofile",
        ".zlogin",
        ".bashrc",
        ".bash_profile",
        ".profile",
        ".tmux.conf",
        ".config/tmux",
        ".gitconfig",
        ".config/git",
        ".config/mise",
        ".control",
        ".control/sockets",
    ],
)
def test_startup_and_socket_parents_are_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    original = host_settings()
    configured = dataclasses.replace(
        original,
        sockets_dir=".control/sockets/private",
        sandbox=dataclasses.replace(original.sandbox, audit_dir=entry),
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(Invalid, match="protected host path"):
        host_configuration.check_config(configured)
