"""Build Claude permissions, sandbox settings, and the agent-model guard."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions.policy_paths import protected_git, resolve
from tbhbdev.alpha.agent.model import ClaudePolicy, Profile

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, RunPaths

_REVIEW_DOMAINS = ("github.com", "api.github.com", "*.githubusercontent.com")
# Claude's file tools, which a review run doesn't get.
_CLAUDE_FILE_TOOLS = ("Edit", "Write", "NotebookEdit")
# Forks always run on the parent's model, so they stay off.
_CLAUDE_SUBAGENT_ENV = {"CLAUDE_CODE_FORK_SUBAGENT": "0"}


def _claude_write_denies(profile: Profile, paths: RunPaths) -> tuple[str, ...]:
    if profile is Profile.BUILD:
        return protected_git(paths)
    return (paths.worktree, *protected_git(paths))


def _guard_hooks(command: str) -> dict[str, object]:
    """The settings' only hook: refuse an Agent launch that names no model.

    A launch may name any model. Hooks stay on for this, so the user's own
    hooks load too. `onFailure: "block"` makes a guard that cannot run (a
    missing binary, a timeout, a crash) refuse the launch instead of
    letting it through.
    """
    handler = {"type": "command", "command": command, "onFailure": "block"}
    return {"PreToolUse": [{"matcher": "Agent", "hooks": [handler]}]}


def guard_command(host: Host, home: str) -> str:
    """Run `tbhbdev alpha guard-agent-model` from the host's configured binary."""
    program = host.tbhbdev or resolve(home, ".local/bin/tbhbdev")
    return f"{shlex.quote(program)} alpha guard-agent-model"


def build(  # noqa: PLR0913 -- one value per policy input
    profile: Profile,
    paths: RunPaths,
    access: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    env: dict[str, str],
    domains: tuple[str, ...],
    *,
    guard: str,
    full: bool,
) -> ClaudePolicy:
    if full:
        # Claude user settings override the process environment.
        return ClaudePolicy(
            {
                "env": {
                    "CLAUDE_CODE_TMPDIR": env["CLAUDE_CODE_TMPDIR"],
                    "GH_CONFIG_DIR": env["GH_CONFIG_DIR"],
                    "DOCKER_CONFIG": env["DOCKER_CONFIG"],
                    **_CLAUDE_SUBAGENT_ENV,
                },
                "hooks": _guard_hooks(guard),
                "sandbox": {"enabled": False},
            }
        )
    writes, deny, protected = access
    review = profile is Profile.REVIEW
    # An absolute path in a Claude rule starts with a second slash.
    denied_tools = [f"{tool}(/{p}/**)" for p in deny for tool in ("Read", "Edit")]
    deny_write = (*_claude_write_denies(profile, paths), *protected)
    # Claude ignores `Write(path)` rules and warns; `Edit(path)` covers Write.
    denied_tools.extend(
        f"Edit(/{p}{suffix})" for p in deny_write for suffix in ("", "/**")
    )
    sandbox: dict[str, object] = {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "enableWeakerNetworkIsolation": True,
        "filesystem": {
            "allowWrite": list(writes),
            "denyRead": list(deny),
            "denyWrite": [*deny_write, *deny],
        },
    }
    network: dict[str, object] = {
        "allowedDomains": list(domains),
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
        "strictAllowlist": True,
    }
    if review:
        network.update(
            {
                "allowedDomains": list(_REVIEW_DOMAINS),
            }
        )
    if paths.signing_socket is not None:
        network["allowUnixSockets"] = [paths.signing_socket]
    sandbox["network"] = network
    run_env = {
        k: env[k]
        for k in (
            "TMPDIR",
            "CLAUDE_CODE_TMPDIR",
            "PATH",
            "TMPPREFIX",
            "GH_CONFIG_DIR",
            "DOCKER_CONFIG",
            "GIT_NO_LAZY_FETCH",
            "PYTEST_DEBUG_TEMPROOT",
            "PYTEST_XDIST_AUTO_NUM_WORKERS",
            "SSH_AUTH_SOCK",
        )
        if k in env
    } | _CLAUDE_SUBAGENT_ENV
    return ClaudePolicy(
        {
            "env": run_env,
            "hooks": _guard_hooks(guard),
            "permissions": {
                "allow": [] if review else [f"Edit(/{p}/**)" for p in writes],
                "deny": [
                    *(_CLAUDE_FILE_TOOLS if review else ()),
                    *denied_tools,
                    "WebFetch",
                    "WebSearch",
                ],
            },
            "sandbox": sandbox,
        }
    )
