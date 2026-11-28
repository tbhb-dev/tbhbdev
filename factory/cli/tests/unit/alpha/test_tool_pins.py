# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Verify the pull request tool pins and the version output they accept."""

import json
import subprocess
import tomllib
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from tbhbdev.alpha import tool_pins

WORKSPACE = next(
    parent.parent
    for parent in Path(__file__).resolve().parents
    if parent.name == "factory"
)


def test_cli_versions_match_the_rules_pins() -> None:
    manifest = Path(__file__).with_name("rules-tool-pins.json")
    if not manifest.exists():
        manifest = WORKSPACE / "lib/rules/pins/tools.json"
    rules = cast("dict[str, dict[str, object]]", json.loads(manifest.read_text()))
    for name in ("gitleaks", "scc", "vale"):
        assert name in tool_pins.PINNED
        assert tool_pins.pinned_version(name) == rules[name]["version"]


def test_cli_prose_versions_match_mise() -> None:
    mise = tomllib.loads((WORKSPACE / "mise.toml").read_text())["tools"]
    for name in ("cspell", "rumdl", "vale"):
        assert name in tool_pins.PINNED
        assert tool_pins.pinned_version(name) == mise[name]


@pytest.mark.parametrize(("name", "prefix"), [("cspell", ""), ("rumdl", "rumdl ")])
def test_prose_tool_version_output(name: str, prefix: str) -> None:
    version = tool_pins.pinned_version(name)
    assert tool_pins.version_problem(name, version, prefix + version) is None
    assert tool_pins.version_problem(name, version, prefix + "0.0.0") == (
        f"reports 0.0.0, expected pinned version {version}"
    )


def _fake_version_run(
    returncode: int = 0, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["tool", "--version"], returncode, stdout, "")


@pytest.mark.parametrize("name", sorted(tool_pins.PINNED))
def test_require_reported_version_accepts_older_version(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(
        tool_pins.subprocess,
        "run",
        Mock(return_value=_fake_version_run(0, f"{name} version 0.0.1\n")),
    )
    assert tool_pins.require_reported_version(name, f"/shim/{name}") == f"/shim/{name}"


def test_require_reported_version_skips_unpinned_tools() -> None:
    assert tool_pins.require_reported_version("cog", "/missing/cog") == "/missing/cog"


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (0, ""),
        (0, "missing version info\n"),
        (0, "not-a-version\n"),
        (2, "10.5.1\n"),
    ],
)
def test_require_reported_version_refuses_no_version(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    monkeypatch.setattr(
        tool_pins.subprocess,
        "run",
        Mock(return_value=_fake_version_run(returncode, stdout)),
    )
    with pytest.raises(SystemExit) as error:
        tool_pins.require_reported_version("cspell", "/shim/cspell")
    assert str(error.value) == "cspell at /shim/cspell reports no version"


def test_require_reported_version_refuses_unstartable_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_pins.subprocess,
        "run",
        Mock(side_effect=FileNotFoundError("no such file")),
    )
    with pytest.raises(SystemExit) as error:
        tool_pins.require_reported_version("cspell", "/shim/cspell")
    assert (
        str(error.value) == "cannot check cspell version at /shim/cspell: no such file"
    )


def test_require_version_refuses_wrong_version_and_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tool_pins.pinned_version("cspell")
    monkeypatch.setattr(
        tool_pins.subprocess,
        "run",
        Mock(return_value=_fake_version_run(0, "10.5.1\n")),
    )
    with pytest.raises(SystemExit) as mismatch:
        tool_pins.require_version("cspell", "/usr/bin/cspell")
    assert str(mismatch.value) == (
        f"cspell at /usr/bin/cspell reports 10.5.1, expected pinned version {expected}"
    )
    monkeypatch.setattr(
        tool_pins.subprocess,
        "run",
        Mock(side_effect=FileNotFoundError("no such file")),
    )
    with pytest.raises(SystemExit) as missing:
        tool_pins.require_version("cspell", "/missing/cspell")
    assert str(missing.value) == (
        f"cannot check cspell version at /missing/cspell (pin {expected}): no such file"
    )
