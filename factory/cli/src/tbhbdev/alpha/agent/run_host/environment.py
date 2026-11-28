"""Build a full-access harness environment and settings from run values."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from tbhbdev.alpha.agent.model import (
    AgyPolicy,
    ClaudePolicy,
    CodexPolicy,
    Harness,
    Host,
    Invalid,
    Os,
    Policy,
    Profile,
    RunPaths,
    RunRequest,
    Settings,
)

LINUX_LANG = "C.UTF-8"
MACOS_LANG = "en_US.UTF-8"
XDIST_WORKERS = "4"
GH_CONFIG_SUBDIR = "gh"
DOCKER_CONFIG_SUBDIR = "docker"
_CLAUDE_SUBAGENT_ENV = {"CLAUDE_CODE_FORK_SUBAGENT": "0"}


def resolve(home: str, path: str) -> str:
    """PATH as an absolute path, resolved against HOME unless it's absolute."""
    return str(PurePosixPath(home) / path)


def tool_path(settings: Settings, host: Host, home: str) -> str:
    search = settings.sandbox.path
    entries = search.linux if host.os is Os.LINUX else search.macos
    return ":".join(resolve(home, e) for e in entries)


def _check(host: Host, *, account: str) -> None:
    if account not in host.accounts:
        message = f"policy account {account!r} isn't one of the accounts of {host.name}"
        raise Invalid(message)


def _guard_hooks(command: str) -> dict[str, object]:
    """The settings' only hook: refuse an Agent launch that names no model.

    A launch may name any model. Hooks stay on for this, so the user's own
    hooks load too. `onFailure: "block"` makes a guard that cannot run (a
    missing binary, a timeout, a crash) refuse the launch instead of
    letting it through.
    """
    handler = {"type": "command", "command": command, "onFailure": "block"}
    return {"PreToolUse": [{"matcher": "Agent", "hooks": [handler]}]}


def _guard_command(host: Host, home: str) -> str:
    """Run `tbhbdev alpha guard-agent-model` from the host's configured binary."""
    program = host.tbhbdev or resolve(home, ".local/bin/tbhbdev")
    return f"{shlex.quote(program)} alpha guard-agent-model"


def environment(
    host: Host, harness: Harness, paths: RunPaths, account: str, tool_search: str
) -> dict[str, str]:
    """The child's whole environment: an allowlist, with no parent values.

    The two config-directory variables name directories under the private
    TMPDIR that run-host creates and fills. They hold no credential.
    """
    env = {
        "PATH": tool_search,
        "HOME": paths.home,
        "USER": account,
        "LOGNAME": account,
        "SHELL": f"/bin/{host.shell}",
        "LANG": LINUX_LANG if host.os is Os.LINUX else MACOS_LANG,
        "TMPDIR": paths.tmpdir,
        "CLAUDE_CODE_TMPDIR": paths.tmpdir,
        "TMPPREFIX": resolve(paths.tmpdir, "zsh"),
        "DOCKER_CONFIG": resolve(paths.tmpdir, DOCKER_CONFIG_SUBDIR),
        "GH_CONFIG_DIR": resolve(paths.tmpdir, GH_CONFIG_SUBDIR),
        "PYTEST_DEBUG_TEMPROOT": paths.tmpdir,
        "PYTEST_XDIST_AUTO_NUM_WORKERS": XDIST_WORKERS,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "7",
    }
    overrides = (
        ("core.fsmonitor", "false"),
        ("safe.bareRepository", "explicit"),
        ("diff.ignoreSubmodules", "all"),
        ("status.submoduleSummary", "false"),
        ("protocol.ext.allow", "never"),
        # `user` lets the run's own `git fetch` read its mirror but refuses the
        # file transport for a submodule or any other git-started fetch.
        ("protocol.file.allow", "user"),
        ("diff.external", ""),
    )
    for index, (key, value) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    if harness is Harness.CODEX:
        env["CODEX_HOME"] = paths.codex_home
    if paths.signing_socket is not None:
        env["SSH_AUTH_SOCK"] = paths.signing_socket
    return env


def _claude(env: dict[str, str], guard: str) -> ClaudePolicy:
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


def build(
    settings: Settings,
    host: Host,
    request: RunRequest,
    paths: RunPaths,
    tool_search: str,
) -> Policy:
    _check(host, account=request.account)
    harness = request.harness
    profile = request.profile
    env = environment(host, harness, paths, request.account, tool_search)
    detail: CodexPolicy | ClaudePolicy | AgyPolicy
    if harness is Harness.CODEX:
        codex = settings.codex
        search = (
            codex.web_search_build
            if profile is Profile.BUILD
            else codex.web_search_review
        )
        detail = CodexPolicy("never", search, codex.subagent_model)
    elif harness is Harness.CLAUDE:
        detail = _claude(env, _guard_command(host, paths.home))
    else:
        detail = AgyPolicy(())
    return Policy(harness, profile, env, detail, tool_search)


def hook_environment(value: Policy) -> dict[str, str]:
    """Reflect the shell overrides each harness applies to its launch environment."""
    if value.harness is Harness.CODEX:
        shell = {
            key: val for key, val in value.env.items() if not key.startswith("GIT_")
        }
        return shell | {"PATH": value.tool_path, "GIT_CONFIG_COUNT": "0"}
    return value.env
