"""Plan run-host inputs, startup paths, and harness launch values."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import config, request
from tbhbdev.alpha.agent.model import Harness, Invalid, RunKind

if TYPE_CHECKING:
    from pathlib import PurePath

    from tbhbdev.alpha.agent.model import Claude, Host, Policy, RunRequest

# These files can alter the next trusted shell or tool invocation.
_STARTUP_FILES = (
    ".zshenv",
    ".zshrc",
    ".zprofile",
    ".zlogin",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".tmux.conf",
    ".config/tmux",
    ".gitconfig",
    ".config/git",
    ".config/mise",
    ".ssh",
    ".claude",
    ".claude.json",
    ".codex",
    ".codex-recovery",
)


def protected_home_paths[P: PurePath](home: P, sockets_dir: str) -> tuple[P, ...]:
    sockets = home / sockets_dir
    # HOME itself is the boundary: protecting it would refuse every workspace.
    return (
        *(home / p for p in _STARTUP_FILES),
        *(p for p in sockets.parents if p.is_relative_to(home) and p != home),
    )


def select_host(hosts: tuple[Host, ...], name: str) -> Host:
    host = next((h for h in hosts if h.name == name), None)
    if host is None:
        message = f"host {name!r} is not configured"
        raise Invalid(message)
    return host


def parse_request(raw: str, host: Host, claude: Claude) -> RunRequest:
    data = json.loads(raw)
    if not isinstance(data, dict):
        message = "request must be a JSON object"
        raise Invalid(message)
    parsed = request.parse(data, host_allows_unsandboxed=host.unsandboxed)
    if parsed.account not in host.accounts:
        message = f"account {parsed.account!r} is not enabled on {host.name}"
        raise Invalid(message)
    if parsed.claude_profile is not None:
        profile = config.find_profile(claude.profiles, parsed.claude_profile)
        if profile is None:
            message = f"claude profile {parsed.claude_profile!r} is not configured"
            raise Invalid(message)
        parsed = dataclasses.replace(parsed, claude_profile=profile.name)
    return parsed


def hook_environment(value: Policy, *, unsandboxed: bool) -> dict[str, str]:
    """Reflect the shell overrides each harness applies to its launch environment."""
    if value.harness is Harness.CODEX:
        shell = {
            key: val for key, val in value.env.items() if not key.startswith("GIT_")
        }
        return shell | {"PATH": value.tool_path, "GIT_CONFIG_COUNT": "0"}
    if value.harness is Harness.CLAUDE and not unsandboxed:
        return value.env | {"PATH": value.tool_path}
    return value.env


def finalize_command(
    command: tuple[str, ...],
    executable: str,
    kind: RunKind,
    harness: Harness,
    final_message: str,
) -> tuple[str, ...]:
    command = (executable, *command[1:])
    if kind is RunKind.EXEC and harness is Harness.CODEX:
        command = (
            *command[:-1],
            "--output-last-message",
            final_message,
            command[-1],
        )
    return command


def agy_project(
    run_name: str, worktree: str, grants: tuple[str, ...], timeout: int
) -> str:
    return json.dumps(
        {
            "name": f"agent-{run_name}",
            "path": worktree,
            "permissions": list(grants),
            "timeout": timeout,
        }
    )
