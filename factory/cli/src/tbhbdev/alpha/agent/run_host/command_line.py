"""Render the argument vector and project contents for a harness."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

from tbhbdev.alpha.agent.model import (
    ClaudePolicy,
    CodexPolicy,
    Invalid,
    Policy,
    Profile,
    RunRequest,
)

_AGY_BUILD_NOTE = (
    "Run shell commands in {worktree} unless the task names another directory."
)
# The variables a harness gets through its own mechanism, in this order.
_HANDOFF = (
    "CODEX_HOME",
    "DOCKER_CONFIG",
    "GH_CONFIG_DIR",
    "PYTEST_DEBUG_TEMPROOT",
    "PYTEST_XDIST_AUTO_NUM_WORKERS",
    "TMPDIR",
    "TMPPREFIX",
)


def _toml(text: str) -> str:
    """TEXT as a TOML basic string, which JSON's escapes also spell."""
    return json.dumps(text)


def _codex(
    request: RunRequest,
    policy: Policy,
    detail: CodexPolicy,
    worktree: str,
) -> tuple[str, ...]:
    argv = ["codex", "exec", "--skip-git-repo-check"]
    # Builds keep their rollout, which the commit hook reads the model from.
    if policy.profile is Profile.REVIEW:
        argv.append("--ephemeral")
    argv += [
        "--strict-config",
        "-C",
        worktree,
        "-s",
        "danger-full-access",
    ]
    # Quoting a path inside a dotted override key loses dots in some CLIs.
    # Override the table as TOML so project trust is decided before layer loading.
    argv += ["-c", f'projects={{{_toml(worktree)}={{trust_level="untrusted"}}}}']
    argv += [
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-c",
        f"approval_policy={_toml(detail.approval_policy)}",
        "-c",
        f"web_search={_toml(detail.web_search.value)}",
    ]
    # A spawn that names a model keeps it. One that names none gets this model
    # and not the run's.
    argv += [
        "-c",
        f"agents.default_subagent_model={_toml(detail.subagent_model)}",
    ]
    argv += ["-c", "features.network_proxy.enabled=false"]
    # A codex policy's environment holds every handoff variable.
    argv += [
        "-c",
        'shell_environment_policy.exclude=["GIT_*"]',
        "-c",
        'shell_environment_policy.set.GIT_CONFIG_COUNT="0"',
        "-c",
        f"shell_environment_policy.set.PATH={_toml(policy.tool_path)}",
    ]
    for name in _HANDOFF:
        argv += ["-c", f"shell_environment_policy.set.{name}={_toml(policy.env[name])}"]
    if "SSH_AUTH_SOCK" in policy.env:
        argv += [
            "-c",
            f"shell_environment_policy.set.SSH_AUTH_SOCK={_toml(policy.env['SSH_AUTH_SOCK'])}",
        ]
    argv += [
        "-m",
        request.model,
        "-c",
        f"model_reasoning_effort={_toml(request.effort)}",
    ]
    argv.append("-")
    return tuple(argv)


def _claude(
    request: RunRequest, detail: ClaudePolicy, stop_hooks: Mapping[str, object]
) -> tuple[str, ...]:
    own = cast("dict[str, object]", detail.settings.get("hooks", {}))
    options = detail.settings | {
        "disableAllHooks": False,
        "hooks": {**own, **stop_hooks},
    }
    settings = json.dumps(options, separators=(",", ":"), sort_keys=True)
    return (
        "claude",
        "-p",
        "--model",
        request.model,
        "--effort",
        request.effort,
        "--permission-mode",
        "bypassPermissions",
        "--settings",
        settings,
        "--setting-sources",
        "",
        "--strict-mcp-config",
    )


def _agy(
    request: RunRequest,
    policy: Policy,
    worktree: str,
    project: str | None,
    brief: str | None,
) -> tuple[str, ...]:
    if project is None:
        message = "argv agy run requires agy_project"
        raise Invalid(message)
    review = policy.profile is Profile.REVIEW
    argv = [
        "agy",
        "--project",
        project,
        "--model",
        request.model,
        "--dangerously-skip-permissions",
    ]
    if review:
        argv += ["--mode", "plan"]
    if brief is None:
        message = "argv agy exec run requires agy_brief"
        raise Invalid(message)
    if request.timeout is None:
        message = "argv agy exec run requires a timeout"
        raise Invalid(message)
    # agy's model once ran commands in a credential directory, so a build's
    # brief starts by naming the worktree.
    text = (
        f"\n{brief}"
        if review
        else f"{_AGY_BUILD_NOTE.format(worktree=worktree)}\n\n{brief}"
    )
    return (*argv, "--print-timeout", f"{request.timeout}s", "--print", text)


def harness(  # noqa: PLR0913 -- one value per input to a harness command
    request: RunRequest,
    policy: Policy,
    *,
    worktree: str,
    executable: str,
    final_message: str,
    stop_hooks: Mapping[str, object],
    agy_project: str | None = None,
    agy_brief: str | None = None,
    schema_file: str | None = None,
    schema_text: str | None = None,
) -> tuple[str, ...]:
    """The harness command of REQUEST, under POLICY, that runs in WORKTREE.

    Codex and Claude read the brief on stdin. agy receives it through `--print`.
    AGY_PROJECT is the id of agy's per-run project.

    Raises:
        Invalid: POLICY was built for another harness or profile than REQUEST's,
            or an agy run lacks its project, its brief or its timeout.
    """
    if policy.harness is not request.harness or policy.profile is not request.profile:
        message = (
            "argv policy was built for another harness or profile than the request's"
        )
        raise Invalid(message)
    detail = policy.detail
    if isinstance(detail, CodexPolicy):
        command = _codex(request, policy, detail, worktree)
        schema = ("--output-schema", schema_file) if schema_file is not None else ()
        return (
            executable,
            *command[1:-1],
            *schema,
            "--output-last-message",
            final_message,
            command[-1],
        )
    if isinstance(detail, ClaudePolicy):
        command = _claude(request, detail, stop_hooks)
        if schema_text is not None:
            return (
                executable,
                *command[1:],
                "--output-format",
                "json",
                "--json-schema",
                schema_text,
            )
        return (executable, *command[1:])
    command = _agy(request, policy, worktree, agy_project, agy_brief)
    return (executable, *command[1:])


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
