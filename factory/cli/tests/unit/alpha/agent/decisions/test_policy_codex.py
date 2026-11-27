"""Codex run profiles and proxy settings."""

from __future__ import annotations

import dataclasses

import pytest
from agent_example import (
    SANDBOX,
    denied,
    paths,
    settings,
)
from policy_example import (
    ACCOUNTS,
    HOSTS,
    dig,
    env_of,
    made,
    strings,
)

from tbhbdev.alpha.agent import host_tools, model
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import (
    Harness,
    Os,
    Profile,
    WebSearch,
)


def codex_expected(
    os: Os, profile: Profile, account: str, search: WebSearch
) -> model.CodexPolicy:
    home = f"/example/home/{account}"
    tmp = f"{home}/.local/state/example-runs/run1.tmp"
    if profile is Profile.BUILD:
        writes = (
            f"{home}/Code/example.com/example-org/example-repo",
            f"{home}/Code/example.com/example-org/example-repo/.git",
            tmp,
            f"{home}/.cache",
            f"{home}/.local/share/mise",
            f"{home}/.local/state",
        )
    else:
        writes = (tmp, f"{home}/.local/state/example-audit")
    if os is Os.MACOS:
        writes = (*writes, "/dev/fd")
    return model.CodexPolicy(
        mode=model.CodexMode.PROFILE,
        read_roots=(
            ":root",
            *(
                f"{home}/Code/example.com/example-org/example-repo/.git/{entry}"
                for entry in (
                    "config",
                    "config.worktree",
                    "commondir",
                    "hooks",
                    "info",
                    "modules",
                )
            ),
        )
        if profile is Profile.BUILD
        else (":root",),
        write_roots=writes,
        deny=denied(os, home),
        approval_policy="never",
        web_search=search,
        network_proxy=policy.network_proxy(settings()),
    )


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_codex_profile(os: Os, profile: Profile, account: str) -> None:
    result = made(os, profile, Harness.CODEX, account)
    assert result.detail == codex_expected(os, profile, account, WebSearch.LIVE)


@pytest.mark.parametrize(
    ("profile", "build", "review"),
    [
        (Profile.BUILD, WebSearch.CACHED, WebSearch.DISABLED),
        (Profile.REVIEW, WebSearch.CACHED, WebSearch.DISABLED),
        (Profile.BUILD, WebSearch.DISABLED, WebSearch.CACHED),
        (Profile.REVIEW, WebSearch.DISABLED, WebSearch.CACHED),
        (Profile.BUILD, WebSearch.LIVE, WebSearch.DISABLED),
        (Profile.REVIEW, WebSearch.DISABLED, WebSearch.LIVE),
    ],
)
def test_web_search_follows_the_profile(
    profile: Profile, build: WebSearch, review: WebSearch
) -> None:
    config = settings(codex=model.Codex(build, review))
    result = made(Os.LINUX, profile, Harness.CODEX, "agent", config=config)
    expected = build if profile is Profile.BUILD else review
    assert isinstance(result.detail, model.CodexPolicy)
    assert result.detail.web_search is expected


def test_a_review_makes_the_worktree_read_only() -> None:
    result = made(Os.LINUX, Profile.REVIEW, Harness.CODEX, "agent")
    assert isinstance(result.detail, model.CodexPolicy)
    worktree = paths("agent").worktree
    assert worktree not in result.detail.write_roots
    assert result.detail.read_roots == (":root",)


@pytest.mark.parametrize("os", Os)
def test_unsandboxed_selects_full_access_for_a_codex_build(os: Os) -> None:
    host = dataclasses.replace(HOSTS[os], unsandboxed=True)
    config = dataclasses.replace(settings(), hosts=(host,))
    result = policy.build(
        config,
        host,
        harness=Harness.CODEX,
        profile=Profile.BUILD,
        account="ops",
        unsandboxed=True,
        paths=paths("ops"),
    )
    assert result.detail == model.CodexPolicy(
        mode=model.CodexMode.FULL_ACCESS,
        read_roots=(),
        write_roots=(),
        deny=(),
        approval_policy="never",
        web_search=WebSearch.LIVE,
        network_proxy=None,
    )
    assert result.env == env_of(os, Harness.CODEX, "ops") | {
        "PATH": ":".join(
            host_tools.account_path(env_of(os, Harness.CODEX, "ops")["HOME"])
        )
    }
    assert result.deny == ()


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("harness", Harness)
def test_dev_fd_is_writable_only_for_codex_and_claude_on_macos(
    os: Os, profile: Profile, harness: Harness
) -> None:
    result = made(os, profile, harness, "agent")
    if isinstance(result.detail, model.CodexPolicy):
        writes = result.detail.write_roots
    elif isinstance(result.detail, model.ClaudePolicy):
        writes = strings(
            dig(result.detail.settings, "sandbox", "filesystem", "allowWrite")
        )
    else:
        assert "write_file(/dev/fd)" not in result.detail.grants
        return
    assert ("/dev/fd" in writes) is (os is Os.MACOS)


@pytest.mark.parametrize("reason", [None, "explicit network exception"])
@pytest.mark.parametrize("domains", [(), ("api.github.com", "example.com")])
@pytest.mark.parametrize("profile", Profile)
def test_codex_proxy_default_and_opt_out(
    reason: str | None, domains: tuple[str, ...], profile: Profile
) -> None:
    configured = settings(
        sandbox=dataclasses.replace(
            SANDBOX, network_domains=domains, codex_network_proxy_opt_out=reason
        )
    )
    detail = made(Os.LINUX, profile, Harness.CODEX, "agent", config=configured).detail
    assert isinstance(detail, model.CodexPolicy)
    assert detail.network_proxy == (
        None
        if reason
        else {
            "enabled": True,
            "mode": "limited",
            "domains": dict.fromkeys(domains, "allow"),
            "unix_sockets": {},
            "dangerously_allow_all_unix_sockets": False,
            "allow_local_binding": False,
        }
    )


@pytest.mark.parametrize("reason", [None, "isolated test host"])
def test_codex_full_access_needs_no_named_opt_out(reason: str | None) -> None:
    configured = settings(
        sandbox=dataclasses.replace(SANDBOX, codex_unsandboxed_opt_out=reason)
    )
    assert configured.sandbox.codex_unsandboxed_opt_out == reason
    value = made(
        Os.LINUX,
        Profile.BUILD,
        Harness.CODEX,
        "agent",
        unsandboxed=True,
        config=configured,
    )
    assert isinstance(value.detail, model.CodexPolicy)
    assert value.detail.mode is model.CodexMode.FULL_ACCESS
    assert value.detail.network_proxy is None


@pytest.mark.parametrize("unsandboxed", [False, True])
def test_codex_carries_the_configured_subagent_model(*, unsandboxed: bool) -> None:
    config = settings(
        codex=model.Codex(WebSearch.LIVE, WebSearch.LIVE, "example-small")
    )
    result = made(
        Os.LINUX,
        Profile.BUILD,
        Harness.CODEX,
        "agent",
        unsandboxed=unsandboxed,
        config=config,
    )
    assert isinstance(result.detail, model.CodexPolicy)
    assert result.detail.subagent_model == "example-small"


def test_codex_defaults_to_the_roster_subagent_model() -> None:
    result = made(Os.LINUX, Profile.BUILD, Harness.CODEX, "agent")
    assert isinstance(result.detail, model.CodexPolicy)
    assert result.detail.subagent_model == "gpt-6.1-sol"
