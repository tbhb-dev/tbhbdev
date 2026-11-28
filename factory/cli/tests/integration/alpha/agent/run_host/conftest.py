"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import os
from pathlib import Path
lazy from collections.abc import Callable

import pytest
from agent_host_example import (
    constant,
    isolate_host_git,
)
from run_host_fixtures import harness_without_push
from runfile_fixtures import agent_suffix_case as agent_suffix_case  # noqa: PLC0414
from test_alpha_agent_credentials import VALUES

from tbhbdev import secrets
from tbhbdev.alpha.agent import (
    admission,
    bazel_servers,
    credentials,
    host_tools,
    push,
    runfiles,
)
from tbhbdev.alpha.agent.run_host import sweep_first


@pytest.fixture
def empty_bazel_listing(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], str]:
    original = bazel_servers.listing
    monkeypatch.setattr(bazel_servers, "listing", constant(""))
    return original


@pytest.fixture(params=["regular", "symlinked"])
def fake_host_bin(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    directory = tmp_path / "host-bin"
    directory.mkdir()
    binary = directory / "multicall"
    binary.write_bytes(b"binary")
    binary.chmod(0o755)
    for name in ("true", "env"):
        path = directory / name
        if request.param == "symlinked":
            path.symlink_to(binary.name)
        else:
            path.write_bytes(b"binary")
            path.chmod(0o755)
    return directory


@pytest.fixture
def fake_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_host_bin: Path
) -> None:
    def read(reference: str) -> str:
        return VALUES[reference]

    def installation(*_args: object) -> str:
        return "example-installation-credential"

    monkeypatch.setenv("HOME", str(runfiles.private_dir(tmp_path / "operator-home")))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in ("CACHE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    isolate_host_git(tmp_path, monkeypatch)
    monkeypatch.setattr(admission, "ROOT", tmp_path / "admission")
    # A launch must never start a real sweep here; test_sweep_first_io restores it.
    monkeypatch.setattr(sweep_first, "run_sweep", constant(None))
    monkeypatch.setattr(secrets, "read", read)
    monkeypatch.setattr(credentials, "installation_token", installation)

    real_resolve = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        if follow_symlinks and name not in (
            "codex",
            "claude",
            "agy",
            "agy-agent",
            "prek",
        ):
            return real_resolve(name, path)
        if Path(name).is_absolute():
            return str(Path(name).resolve() if follow_symlinks else Path(name))
        return str(fake_host_bin / ("env" if name == "env" else "true"))

    binary = fake_host_bin / "codex"
    binary.write_bytes(b"binary")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_host_bin}:{os.environ['PATH']}")
    monkeypatch.setattr(host_tools, "resolve", resolve)


@pytest.fixture
def portable_run_protocol(
    monkeypatch: pytest.MonkeyPatch, agent_suffix_case: None
) -> None:
    """Check protocol spelling even on a case-insensitive filesystem."""
    del agent_suffix_case
    original_copy = runfiles.copy_final

    def copy(source: Path, destination: Path) -> None:
        assert source.name == "out.final"
        assert destination.name.endswith(".out.final")
        original_copy(source, destination)

    monkeypatch.setattr(runfiles, "copy_final", copy)


@pytest.fixture
def stub_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push, "run", harness_without_push)
