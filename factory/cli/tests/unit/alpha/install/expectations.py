"""Expected installer output shared by configuration tests."""

import tomllib
from typing import TYPE_CHECKING

from tbhbdev.alpha import gh_shim
from tbhbdev.alpha.install import hooks, trust

if TYPE_CHECKING:
    from pathlib import Path


def marked(text: str) -> str:
    return f"tbhbdev alpha {text} {hooks.MARK}"


def trust_hash(label: str, matcher: str | None, text: str) -> str:
    return trust.hook_hash(label, matcher, marked(text), 600)


def trusted_in(path: Path) -> dict[str, str]:
    state = tomllib.loads(path.read_text())["hooks"]["state"]
    return {key: entry["trusted_hash"] for key, entry in state.items()}


def our_trust(hooks: Path) -> dict[str, str]:
    """The trust a fresh Codex install earns, spelled out from the hooks."""
    return {
        f"{hooks}:stop:0:0": trust_hash("stop", None, "run-stop-hook codex"),
        f"{hooks}:session_start:0:0": trust_hash(
            "session_start", None, "identity record codex"
        ),
        f"{hooks}:subagent_start:0:0": trust_hash(
            "subagent_start", None, "identity record codex"
        ),
        f"{hooks}:post_tool_use:0:0": trust_hash(
            "post_tool_use", "apply_patch", "identity record codex"
        ),
        f"{hooks}:post_tool_use:1:0": trust_hash(
            "post_tool_use", "apply_patch", "pr hook codex"
        ),
        f"{hooks}:pre_tool_use:0:0": trust_hash(
            "pre_tool_use", "^Bash$", "guard-gh-pr"
        ),
        f"{hooks}:pre_tool_use:1:0": trust_hash(
            "pre_tool_use", "^Bash$", "guard-gh-poll"
        ),
        f"{hooks}:pre_tool_use:2:0": trust_hash(
            "pre_tool_use", "^Bash$", "guard-scripts"
        ),
        f"{hooks}:pre_tool_use:3:0": trust_hash(
            "pre_tool_use", "^apply_patch$", "guard-scripts"
        ),
        f"{hooks}:pre_tool_use:4:0": trust_hash(
            "pre_tool_use", "^(Bash|shell)$", "guard-op"
        ),
        f"{hooks}:pre_tool_use:5:0": trust_hash(
            "pre_tool_use", "apply_patch", "guard-op"
        ),
        f"{hooks}:pre_tool_use:6:0": trust_hash(
            "pre_tool_use", "^Bash$", "guard-gh-config-dir"
        ),
    }


FOREIGN_CONFIG = """# Codex settings
model = "example"  # the model

[mcp_servers.docs]
command = "docs-server"

[hooks.state."/elsewhere/hooks.json:pre_tool_use:0:0"]
trusted_hash = "sha256:foreign"

# profiles
[profiles.fast]
effort = "low"
"""


def shim_path() -> Path:
    return gh_shim.bin_dir() / "gh"


THEIRS = {"type": "command", "command": "their-hook"}
