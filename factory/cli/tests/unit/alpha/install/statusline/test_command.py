"""Tests for building and recognizing a pinned status line command."""

import shlex
from pathlib import PurePath

import pytest

from tbhbdev.alpha.install.statusline import command as decisions

INSTALL_DIR = PurePath("/data/tbhbdev/ccstatusline")
ROOT = INSTALL_DIR / "2.2.33"
OLD, BARE = decisions.OLD_STATUSLINES


def test_ours_is_the_installers_own_command_for_any_node_and_version() -> None:
    for version in ("2.2.33", "2.3.0"):
        script = decisions.statusline_script(INSTALL_DIR / version)
        assert decisions.is_ours(
            decisions.statusline_command("/a b/node", script), INSTALL_DIR
        )
    script = decisions.statusline_script(ROOT)
    assert decisions.is_ours(
        decisions.statusline_command("/x/bin/node", script), INSTALL_DIR
    )


@pytest.mark.parametrize(
    "node", ["node", "evil-binary", "$MY_STATUS_NODE", "/x/bin/evil", "~/node"]
)
def test_a_node_slot_other_than_an_absolute_node_is_not_ours(node: str) -> None:
    script = decisions.statusline_script(ROOT)
    assert not decisions.is_ours(
        f"{decisions.COLUMNS_PREFIX} {node} {shlex.quote(str(script))}", INSTALL_DIR
    )


def test_a_command_not_in_the_installers_quoting_or_place_is_not_ours() -> None:
    script = decisions.statusline_script(ROOT)
    head = f"{decisions.COLUMNS_PREFIX} "
    assert not decisions.is_ours(f"{head}/x/node {script}   ", INSTALL_DIR)
    assert not decisions.is_ours(
        f'{head}"/x/node" {shlex.quote(str(script))}', INSTALL_DIR
    )
    assert not decisions.is_ours(
        f"{head}/x/node {shlex.quote(str(script))} --extra", INSTALL_DIR
    )
    assert not decisions.is_ours(
        f"{head}/x/node /elsewhere/dist/ccstatusline.js", INSTALL_DIR
    )
    other = PurePath("/elsewhere/2.2.33/node_modules/ccstatusline/dist/ccstatusline.js")
    assert not decisions.is_ours(
        decisions.statusline_command("/x/node", other), INSTALL_DIR
    )
    assert not decisions.is_ours(
        f"x{head}/x/node {shlex.quote(str(script))}", INSTALL_DIR
    )


def test_a_command_with_an_unbalanced_quote_is_not_ours() -> None:
    script = decisions.statusline_script(ROOT)
    other = f"{decisions.COLUMNS_PREFIX} 'node {shlex.quote(str(script))}"
    assert not decisions.is_ours(other, INSTALL_DIR)


@pytest.mark.parametrize(
    "config",
    [
        {"model": "opus"},
        {"statusLine": "text"},
        {"statusLine": {"type": "command"}},
        {"statusLine": {"type": "command", "command": 5}},
        {"statusLine": {"type": "command", "command": "~/bin/my-line"}},
        {"statusLine": {"type": "command", "command": f"{BARE} --json"}},
        {"statusLine": {"type": "command", "command": "bunx ccstatusline@2.2.0"}},
    ],
)
def test_a_custom_or_missing_statusline_is_not_replaceable(
    config: decisions.Json,
) -> None:
    assert decisions.replaceable_line(config, INSTALL_DIR) is None


@pytest.mark.parametrize(
    "command",
    [
        OLD,
        BARE,
        decisions.statusline_command("/bin/node", decisions.statusline_script(ROOT)),
    ],
)
def test_replaceable_line_keeps_the_other_fields(command: str) -> None:
    line = {"type": "command", "command": command, "padding": 0}
    assert decisions.replaceable_line({"statusLine": line}, INSTALL_DIR) == line


def test_the_package_lives_in_node_modules_under_the_root() -> None:
    assert decisions.statusline_package(ROOT).relative_to(ROOT).parts == (
        "node_modules",
        "ccstatusline",
    )


def test_the_command_quotes_its_paths_and_keeps_the_width_fallback() -> None:
    text = decisions.statusline_command("/a b/node", PurePath("/c d/s.js"))
    assert text == f"{decisions.COLUMNS_PREFIX} '/a b/node' '/c d/s.js'"
    assert BARE == "bunx -y ccstatusline@latest"
    assert (
        decisions.COLUMNS_PREFIX
        == "COLUMNS=${COLUMNS:-$(tmux display-message -p '#{pane_width}' 2>/dev/null"
        " || echo 80)}"
    )
    assert f"PATH=/opt/homebrew/bin:$PATH {decisions.COLUMNS_PREFIX} {BARE}" == OLD
    assert decisions.statusline_script(PurePath("/r")) == PurePath(
        "/r/node_modules/ccstatusline/dist/ccstatusline.js"
    )


@pytest.mark.parametrize(
    ("node", "bun", "installed", "missing"),
    [
        (None, None, False, "node"),
        (None, "/x/bun", True, "node"),
        ("/x/node", None, True, None),
        ("/x/node", None, False, "bun"),
        ("/x/node", "/x/bun", False, None),
    ],
)
def test_missing_tool(
    node: str | None, bun: str | None, installed: bool, missing: str | None
) -> None:
    assert decisions.missing_tool(node, bun, installed=installed) == missing
