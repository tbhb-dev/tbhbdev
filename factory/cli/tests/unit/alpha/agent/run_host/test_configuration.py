"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
lazy from pathlib import Path

import pytest
from agent_host_example import (
    host_settings,
    make_run,
)

from tbhbdev.alpha.agent.model import (
    Invalid,
)
from tbhbdev.alpha.agent.run_host import configuration as host_configuration

pytestmark = pytest.mark.usefixtures(
    "empty_bazel_listing", "fake_secrets", "portable_run_protocol", "stub_push"
)


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
def test_startup_and_socket_parents_are_protected(tmp_path: Path, entry: str) -> None:
    run = make_run(tmp_path)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sockets_dir=".control/sockets/private",
            sandbox=dataclasses.replace(run.settings.sandbox, audit_dir=entry),
        ),
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("HOME", str(run.home))
        with pytest.raises(Invalid, match="protected host path"):
            host_configuration.check_config(run.settings)
