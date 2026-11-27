# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Path and command decisions for the pinned ccstatusline install."""

import shlex
from pathlib import PurePath

COLUMNS_PREFIX = (
    "COLUMNS=${COLUMNS:-$(tmux display-message -p '#{pane_width}' 2>/dev/null"
    " || echo 80)}"
)
BUNX_COMMAND = "bunx -y ccstatusline@latest"
# The two forms of the registry-resolving command this installer replaces.
OLD_STATUSLINES = (
    f"PATH=/opt/homebrew/bin:$PATH {COLUMNS_PREFIX} {BUNX_COMMAND}",
    BUNX_COMMAND,
)
STATUSLINE_TOKENS = 2  # the node path and the script


def statusline_package[P: PurePath](root: P) -> P:
    """The installed package's directory."""
    return root / "node_modules" / "ccstatusline"


def statusline_script[P: PurePath](root: P) -> P:
    """The package's bin, `dist/ccstatusline.js` in its package.json."""
    return statusline_package(root) / "dist" / "ccstatusline.js"


def statusline_command(node: str, script: PurePath) -> str:
    """The pinned command with the installer's quoting and column fallback."""
    return f"{COLUMNS_PREFIX} {shlex.quote(node)} {shlex.quote(str(script))}"


def missing_tool(node: str | None, bun: str | None, *, installed: bool) -> str | None:
    """Name the tool the pin can't run without, or None when it has both."""
    if not node:
        return "node"
    return "bun" if not installed and not bun else None


def is_ours(command: str, versions_dir: PurePath) -> bool:
    """Whether the command matches the installer format for any pinned version.

    The command must use the installer's quoting, an absolute path to `node`,
    and the package script under a version directory in `versions_dir`.
    """
    head = f"{COLUMNS_PREFIX} "
    if not command.startswith(head):
        return False
    try:
        tokens = shlex.split(command[len(head) :])
    except ValueError:
        return False
    if len(tokens) != STATUSLINE_TOKENS:
        return False
    node, script = PurePath(tokens[0]), PurePath(tokens[1])
    return (
        node.is_absolute()
        and node.name == "node"
        and script.is_absolute()
        # Any root gives the same last four parts, so mutating "/" changes nothing.
        and script.parts[-4:]
        == statusline_script(PurePath("/")).parts[-4:]  # pragma: no mutate
        and script.parents[3].parent == versions_dir
        and command == statusline_command(tokens[0], script)
    )


def replaceable_line(
    config: dict[str, object], versions_dir: PurePath
) -> dict[str, object] | None:
    """The `statusLine` object when its command is a known or our own one."""
    line = config.get("statusLine")
    if not isinstance(line, dict):
        return None
    command = line.get("command")
    if not isinstance(command, str):
        return None
    known = command in OLD_STATUSLINES or is_ours(command, versions_dir)
    return line if known else None
