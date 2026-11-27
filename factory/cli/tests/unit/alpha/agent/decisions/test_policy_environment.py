"""Run environments and tool paths."""

from __future__ import annotations

import dataclasses

import pytest
from agent_example import (
    LINUX,
    MACOS,
    paths,
    settings,
)
from policy_example import (
    COMBINATIONS,
    IDS,
    PATH,
    claude_settings_of,
    dig,
    env_of,
    made,
)

from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.model import (
    Harness,
    Os,
    Profile,
)


@pytest.mark.parametrize(("os", "profile", "harness", "account"), COMBINATIONS, ids=IDS)
def test_environment_is_exactly_the_allowlist(
    os: Os,
    profile: Profile,
    harness: Harness,
    account: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No parent value reaches the child: the function never receives one.
    for name in (
        "PATH",
        "HOME",
        "TMPDIR",
        "CLAUDE_CODE_TMPDIR",
        "TMPPREFIX",
        "GH_CONFIG_DIR",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TERM",
        "EXAMPLE_PARENT",
        "DEVELOPER_DIR",
    ):
        monkeypatch.setenv(name, "/parent-value")
    result = made(os, profile, harness, account)
    assert result.env == env_of(os, harness, account)
    assert result.env["TMPPREFIX"] == result.env["TMPDIR"] + "/zsh"
    assert result.env["CLAUDE_CODE_TMPDIR"] == result.env["TMPDIR"]


@pytest.mark.parametrize("host", [LINUX, MACOS])
def test_parent_and_tool_search_paths_are_separate(host: model.Host) -> None:
    result = made(host.os, Profile.BUILD, Harness.CODEX, "agent")
    fixed = "/usr/bin:/bin:/usr/sbin:/sbin"
    if host.os is Os.MACOS:
        fixed += ":/opt/homebrew/bin:/usr/local/bin"
    assert result.env["PATH"] == fixed
    assert result.tool_path == PATH[host.os].format(home="/example/home/agent")


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
@pytest.mark.parametrize("os_name", Os)
def test_harness_parent_neutralizes_all_repository_git_config(
    harness: Harness, os_name: Os
) -> None:
    result = made(os_name, Profile.BUILD, harness, "agent")
    assert result.env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert result.env["GIT_CONFIG_COUNT"] == "7"
    assert {
        result.env[f"GIT_CONFIG_KEY_{i}"]: result.env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(7)
    } == {
        "core.fsmonitor": "false",
        "diff.ignoreSubmodules": "all",
        "status.submoduleSummary": "false",
        "protocol.ext.allow": "never",
        "protocol.file.allow": "user",
        "diff.external": "",
        "safe.bareRepository": "explicit",
    }


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("configured", [False, True])
def test_github_config_for_every_run(
    harness: Harness,
    profile: Profile,
    configured: bool,
) -> None:
    original = settings()
    config = dataclasses.replace(
        original,
        credentials=tuple(
            dataclasses.replace(c, gh=c.gh if configured else None)
            for c in original.credentials
        ),
    )
    result = made(Os.LINUX, profile, harness, "agent", config=config)
    gh = f"{paths('agent').tmpdir}/gh"
    assert result.env["GH_CONFIG_DIR"] == gh
    if harness is Harness.CLAUDE:
        claude_env = dig(claude_settings_of(result), "env")
        assert isinstance(claude_env, dict)
        assert claude_env["GH_CONFIG_DIR"] == gh
    if isinstance(result.detail, model.AgyPolicy):
        assert f"read_file({gh})" in result.detail.grants
