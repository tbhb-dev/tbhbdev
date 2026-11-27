# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Verify the pull request tool pins and the version output they accept."""

import json
import tomllib
from pathlib import Path
from typing import cast

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
