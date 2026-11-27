"""The sandbox policy of a run: what its harness may touch, and its environment.

`build` is a pure function from the settings, the host, the run's choices and
the run's paths to a frozen `Policy`. It reads nothing, checks no path on disk
and never receives a parent environment. Every path in the policy is absolute,
and a config path that isn't absolute resolves against the run account's home.
There are no default lists here: write roots, denied paths and `PATH` come from
`[agent.sandbox]`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.decisions import policy_agy, policy_claude, policy_codex
from tbhbdev.alpha.agent.decisions.policy_codex import (
    APPROVAL_POLICY,
    READ_ROOT,
    network_proxy,
)
from tbhbdev.alpha.agent.decisions.policy_environment import (
    DOCKER_CONFIG_SUBDIR,
    GH_CONFIG_SUBDIR,
    LINUX_LANG,
    MACOS_LANG,
    XDIST_WORKERS,
    environment,
    review_environment,
    tool_path,
)
from tbhbdev.alpha.agent.decisions.policy_paths import (
    DEV_FD,
    GIT_CONFIG_PATHS,
    denied_paths,
    protect_host,
    protect_paths,
    protected_git,
    refuse_overlaps,
    resolve,
    write_roots,
)
from tbhbdev.alpha.agent.model import AgyPolicy, Harness, Invalid, Os, Policy, Profile

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import (
        ClaudePolicy,
        CodexPolicy,
        Host,
        RunPaths,
        Settings,
    )

__all__ = [
    "APPROVAL_POLICY",
    "DOCKER_CONFIG_SUBDIR",
    "GH_CONFIG_SUBDIR",
    "GIT_CONFIG_PATHS",
    "LINUX_LANG",
    "MACOS_LANG",
    "READ_ROOT",
    "XDIST_WORKERS",
    "build",
    "denied_paths",
    "environment",
    "network_proxy",
    "protect_host",
    "protect_paths",
    "protected_git",
    "resolve",
    "review_environment",
    "tool_path",
]


def _check(host: Host, *, account: str) -> None:
    if account not in host.accounts:
        message = f"policy account {account!r} isn't one of the accounts of {host.name}"
        raise Invalid(message)


def build(  # noqa: PLR0913 -- one value per fact the policy depends on
    settings: Settings,
    host: Host,
    *,
    harness: Harness,
    profile: Profile,
    account: str,
    unsandboxed: bool,
    paths: RunPaths,
    protected: tuple[str, ...] = (),
    denied: tuple[str, ...] | None = None,
) -> Policy:
    """The policy of a run of HARNESS with PROFILE on HOST, as ACCOUNT.

    UNSANDBOXED disables the harness sandbox for either profile.
    A build run writes the worktree, the private
    TMPDIR and the config's `write_roots`. A review run reads the worktree and
    writes the private TMPDIR and the audit directory.

    Raises:
        Invalid: ACCOUNT isn't one of the host's accounts, or a sandboxed
            write path, signing socket or an agy read grant reaches a denied path.
    """
    _check(host, account=account)
    if unsandboxed:
        env = environment(settings, host, harness, paths, account) | review_environment(
            profile, paths
        )
        tools_path = ":".join(host_tools.account_path(paths.home))
        env["PATH"] = tools_path
        if harness is Harness.CODEX:
            detail = policy_codex.build(settings, profile, (), (), full=True)
        elif harness is Harness.CLAUDE:
            detail = policy_claude.build(
                profile,
                paths,
                ((), (), ()),
                env,
                (),
                guard=policy_claude.guard_command(host, paths.home),
                full=True,
            )
        else:
            detail = AgyPolicy(())
        return Policy(harness, profile, env, (), detail, tools_path)
    protect_host(settings, paths, protected)
    deny = (
        denied_paths(settings, host, paths.home, paths.uid)
        if denied is None
        else denied
    )
    if paths.signing_socket is not None:
        refuse_overlaps((paths.signing_socket,), deny, "signing socket")
    env = environment(settings, host, harness, paths, account) | review_environment(
        profile, paths
    )
    tools_path = tool_path(settings, host, paths.home)
    writes = write_roots(settings, profile, paths)
    if host.os is Os.MACOS and harness in (Harness.CODEX, Harness.CLAUDE):
        writes = tuple(dict.fromkeys((*writes, DEV_FD)))
    agy_writes = (
        policy_agy.write_roots(settings, profile, paths)
        if harness is Harness.AGY
        else ()
    )
    refuse_overlaps((*writes, *agy_writes), deny, "write path")
    detail: CodexPolicy | ClaudePolicy | AgyPolicy
    if harness is Harness.CODEX:
        codex_detail = policy_codex.build(settings, profile, writes, deny, full=False)
        if profile is Profile.BUILD:
            codex_detail = replace(
                codex_detail,
                read_roots=(
                    *codex_detail.read_roots,
                    *protected_git(paths)[1:],
                ),
                write_roots=(
                    paths.worktree,
                    resolve(paths.worktree, ".git"),
                    *codex_detail.write_roots[1:],
                ),
            )
        detail = codex_detail
    elif harness is Harness.CLAUDE:
        detail = policy_claude.build(
            profile,
            paths,
            (writes, deny, protected),
            env | {"PATH": tools_path},
            settings.sandbox.network_domains,
            guard=policy_claude.guard_command(host, paths.home),
            full=False,
        )
    else:
        detail = policy_agy.build(settings, profile, paths, env, deny)
    return Policy(harness, profile, env, deny, detail, tools_path)
