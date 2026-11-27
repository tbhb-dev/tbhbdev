"""Build and recognize the command for the pinned Claude status line."""

import shlex
from pathlib import PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tbhbdev.alpha.install.model import Json

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


def statusline_package[PathT: PurePath](root: PathT) -> PathT:
    """The installed package's directory."""
    return root / "node_modules" / "ccstatusline"


def statusline_script[PathT: PurePath](root: PathT) -> PathT:
    """The package's bin, `dist/ccstatusline.js` in its package.json."""
    return statusline_package(root) / "dist" / "ccstatusline.js"


def statusline_command(node: str, script: PurePath) -> str:
    return f"{COLUMNS_PREFIX} {shlex.quote(node)} {shlex.quote(str(script))}"


def is_ours(command: str, install_dir: PurePath) -> bool:
    """Whether COMMAND is exactly what this installer writes, for any node and version.

    The node slot must be an absolute path named `node`, the script the pinned
    package's bin under a version directory within INSTALL_DIR, and the
    quoting the installer's own. So `$MY_STATUS_NODE` and `evil-binary` don't match,
    and a command written for an older version does.
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
        and script.parents[3].parent == install_dir
        and command == statusline_command(tokens[0], script)
    )


def replaceable_line(config: Json, install_dir: PurePath) -> Json | None:
    """The `statusLine` object when its command is a known or our own one."""
    line = config.get("statusLine")
    if not isinstance(line, dict):
        return None
    command = line.get("command")
    if not isinstance(command, str):
        return None
    known = command in OLD_STATUSLINES or is_ours(command, install_dir)
    return line if known else None


def missing_tool(node: str | None, bun: str | None, *, installed: bool) -> str | None:
    """Name the runtime or installer needed for a usable status line."""
    if not node:
        return "node"
    if not installed and not bun:
        return "bun"
    return None


def pinned_settings(config: Json, install_dir: PurePath, command: str) -> Json | None:
    """Set the command if the current status line is still one we can replace."""
    line = replaceable_line(config, install_dir)
    if line is None:
        return None
    return {**config, "statusLine": {**line, "command": command}}
