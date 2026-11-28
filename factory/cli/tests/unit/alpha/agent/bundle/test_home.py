"""Bundle home locates the shipped CLI folder from the package file."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.agent.bundle import home

if TYPE_CHECKING:
    from collections.abc import Callable


def _constant(value: str | None) -> Callable[[], str | None]:
    def _get() -> str | None:
        return value

    return _get


def test_bundled_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        home,
        "module_file",
        _constant("/state/tbhbdev/agent-runs/run-1.cli/lib/tbhbdev/__init__.py"),
    )
    bundle = Path("/state/tbhbdev/agent-runs/run-1.cli")
    assert home.root() == bundle
    assert home.bin_dir() == bundle / "bin"
    assert home.program() == str(bundle / "bin" / "tbhbdev")


def test_bundled_paths_with_dots_and_nested_lib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        home,
        "module_file",
        _constant("/agent-runs/run.1.cli/lib/nested/tbhbdev/__init__.py"),
    )
    bundle = Path("/agent-runs/run.1.cli")
    assert home.root() == bundle
    assert home.bin_dir() == bundle / "bin"
    assert home.program() == str(bundle / "bin" / "tbhbdev")


@pytest.mark.parametrize(
    "location",
    [
        None,
        "",
        "agent-runs/run-1.cli/lib/tbhbdev/__init__.py",
        "/home/agent/tbhbdev/src/tbhbdev/__init__.py",
        "/state/other/run-1.cli/lib/tbhbdev/__init__.py",
        "/state/agent-runs/nested/run-1.cli/lib/tbhbdev/__init__.py",
        "/state/agent-runs/run-1/lib/tbhbdev/__init__.py",
        "/state/agent-runs/.cli/lib/tbhbdev/__init__.py",
        "/state/agent-runs/run-1.cli/venv/lib/python3.15/site-packages/tbhbdev/__init__.py",
        "/state/agent-runs/run-1.cli/bin/tbhbdev",
        "/state/agent-runs/run-1.cli/lib",
    ],
)
def test_unbundled_paths(location: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home, "module_file", _constant(location))
    assert home.root() is None
    assert home.bin_dir() is None
    assert home.program() == "tbhbdev"


def test_default_module_file_is_unbundled() -> None:
    assert home.root() is None
    assert home.bin_dir() is None
    assert home.program() == "tbhbdev"
