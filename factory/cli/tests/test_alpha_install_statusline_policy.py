# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# gazelle:include_pytest_conftest false

"""Tests for ccstatusline path and command decisions."""

import shlex
from pathlib import PurePath

import pytest

from tbhbdev.alpha import install_statusline_policy as policy

VERSIONS = PurePath("/data/tbhbdev/ccstatusline")
OLD, BARE = policy.OLD_STATUSLINES


def test_ours_is_the_installers_own_command_for_any_node_and_version() -> None:
    for version in ("2.2.33", "2.3.0"):
        script = policy.statusline_script(VERSIONS / version)
        assert policy.is_ours(policy.statusline_command("/a b/node", script), VERSIONS)
    script = policy.statusline_script(VERSIONS / "2.2.33")
    assert policy.is_ours(policy.statusline_command("/x/bin/node", script), VERSIONS)


@pytest.mark.parametrize(
    "node", ["node", "evil-binary", "$MY_STATUS_NODE", "/x/bin/evil", "~/node"]
)
def test_a_node_slot_other_than_an_absolute_node_is_not_ours(node: str) -> None:
    script = policy.statusline_script(VERSIONS / "2.2.33")
    assert not policy.is_ours(
        f"{policy.COLUMNS_PREFIX} {node} {shlex.quote(str(script))}", VERSIONS
    )


def test_a_command_not_in_the_installers_quoting_or_place_is_not_ours() -> None:
    script = policy.statusline_script(VERSIONS / "2.2.33")
    head = f"{policy.COLUMNS_PREFIX} "
    assert not policy.is_ours(f"{head}/x/node {script}   ", VERSIONS)
    assert not policy.is_ours(f'{head}"/x/node" {shlex.quote(str(script))}', VERSIONS)
    assert not policy.is_ours(
        f"{head}/x/node {shlex.quote(str(script))} --extra", VERSIONS
    )
    assert not policy.is_ours(
        f"{head}/x/node /elsewhere/dist/ccstatusline.js", VERSIONS
    )
    other = PurePath("/elsewhere/2.2.33/node_modules/ccstatusline/dist/ccstatusline.js")
    assert not policy.is_ours(policy.statusline_command("/x/node", other), VERSIONS)
    assert not policy.is_ours(f"x{head}/x/node {shlex.quote(str(script))}", VERSIONS)


def test_the_package_lives_in_node_modules_under_the_root() -> None:
    root = PurePath("/r")
    assert policy.statusline_package(root).relative_to(root).parts == (
        "node_modules",
        "ccstatusline",
    )


def test_the_command_quotes_its_paths_and_keeps_the_width_fallback() -> None:
    text = policy.statusline_command("/a b/node", PurePath("/c d/s.js"))
    assert text == f"{policy.COLUMNS_PREFIX} '/a b/node' '/c d/s.js'"
    assert BARE == "bunx -y ccstatusline@latest"
    assert (
        policy.COLUMNS_PREFIX
        == "COLUMNS=${COLUMNS:-$(tmux display-message -p '#{pane_width}' 2>/dev/null"
        " || echo 80)}"
    )
    assert f"PATH=/opt/homebrew/bin:$PATH {policy.COLUMNS_PREFIX} {BARE}" == OLD
    assert policy.statusline_script(PurePath("/r")) == PurePath(
        "/r/node_modules/ccstatusline/dist/ccstatusline.js"
    )
