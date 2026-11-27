"""The argv builders use literal values and an isolated session-key path."""

from __future__ import annotations

import dataclasses
from unittest.mock import patch
lazy from pathlib import Path

import pytest
from agent_example import (
    LINUX,
    MACOS,
    trusted_executables,
)

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.model import Invalid

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107


@pytest.fixture(autouse=True)
def fixed_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    trusted_executables(monkeypatch)


@pytest.mark.parametrize("hostname", ["example-local", "example-local.example.com"])
@pytest.mark.parametrize("key_kind", ["missing", "file", "directory"])
def test_ssh(isolated_home: Path, hostname: str, key_kind: str) -> None:
    key = isolated_home / ".ssh" / "sessions" / "example-local_ed25519"
    key.parent.mkdir(parents=True)
    # Another machine's key must never be selected as a fallback.
    (key.parent / "example-other_ed25519").touch()
    if key_kind == "file":
        key.touch()
    elif key_kind == "directory":
        key.mkdir()
    with patch("socket.gethostname", return_value=hostname):
        result = transport.ssh_argv(
            LINUX,
            ("example-tool", "run", "a b"),
            connect_timeout=15,
            keepalive=30,
        )
    identity = (
        ("-i", str(key), "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes")
        if key_kind == "file"
        else ()
    )
    assert result == (
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        *identity,
        "-l",
        "agent",
        "example-linux",
        "/bin/bash -c 'example-tool run '\"'\"'a b'\"'\"''",
    )


def test_ssh_uses_the_hosts_shell() -> None:
    result = transport.ssh_argv(MACOS, ("true",), connect_timeout=1, keepalive=2)
    assert result[-2:] == ("example-macos", "/bin/zsh -c true")


@pytest.mark.parametrize("account", ["agent", "ops"])
@pytest.mark.parametrize("count", [1, 5])
def test_ssh_account_and_keepalive_count(account: str, count: int) -> None:
    host = dataclasses.replace(LINUX, account=account)
    result = transport.ssh_argv(
        host, ("true",), connect_timeout=2, keepalive=7, keepalive_count_max=count
    )
    assert result == (
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=2",
        "-o",
        "ServerAliveInterval=7",
        "-o",
        f"ServerAliveCountMax={count}",
        "-l",
        account,
        "example-linux",
        "/bin/bash -c true",
    )


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_executables_resolve_only_fixed_path(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    name = "ssh"
    monkeypatch.setattr(transport.sys, "platform", platform)
    expected = "/usr/bin:/bin:/usr/sbin:/sbin"
    if platform == "darwin":
        expected += ":/opt/homebrew/bin:/usr/local/bin"

    def which(program: str, *, path: str) -> str:
        assert program == name
        assert path == expected
        return "/usr/bin/" + name

    monkeypatch.setattr(transport.shutil, "which", which)
    assert transport.executable("ssh") == "/usr/bin/" + name

    def missing(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(transport.shutil, "which", missing)
    with pytest.raises(
        Invalid, match=f"required executable '{name}' missing from fixed PATH"
    ):
        transport.executable("ssh")
