# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""The GitHub App environment and OpenSSL runner."""

import subprocess
import time
from pathlib import Path

import pytest

from tbhbdev import secrets
from tbhbdev.alpha import (
    gh_app_runtime,
)
from tbhbdev.alpha.agent import host_tools


def test_state_root() -> None:
    home = Path("/home/u")
    assert gh_app_runtime.state_root({"XDG_STATE_HOME": "/x"}, home) == Path(
        "/x/gh-app"
    )
    assert gh_app_runtime.state_root({}, home) == Path("/home/u/.local/state/gh-app")
    assert gh_app_runtime.state_root({"XDG_STATE_HOME": ""}, home) == Path(
        "/home/u/.local/state/gh-app"
    )


def test_default_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    env = gh_app_runtime.default_env()
    assert env.state == tmp_path / "gh-app"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert gh_app_runtime.default_env().state == tmp_path / "home/.local/state/gh-app"
    assert env.store is secrets.Store
    assert env.openssl is gh_app_runtime.run_openssl
    assert env.transport is None
    assert abs(env.now() - time.time()) < 5


def test_run_openssl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    done = gh_app_runtime.run_openssl(["dgst", "-sha256"], b"abc")
    assert done.returncode == 0
    assert (
        b"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        in done.stdout
    )
    assert (
        gh_app_runtime.run_openssl(
            ["pkey", "-in", str(tmp_path / "none")], b""
        ).returncode
        != 0
    )
    monkeypatch.setattr(host_tools, "search", lambda: str(tmp_path))
    with pytest.raises(gh_app_runtime.GhAppError, match=r"^openssl isn't installed$"):
        gh_app_runtime.run_openssl(["version"], b"")


def test_run_openssl_runs_openssl_with_its_input_and_captures_its_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(
        argv: list[str], **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gh_app_runtime.run_openssl(["version"], b"in")
    gh_app_runtime.run_openssl(["version"])
    assert calls == [
        (
            [host_tools.resolve("openssl"), "version"],
            {"input": b"in", "capture_output": True, "check": False},
        ),
        (
            [host_tools.resolve("openssl"), "version"],
            {"input": None, "capture_output": True, "check": False},
        ),
    ]
