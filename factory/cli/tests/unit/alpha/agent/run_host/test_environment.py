"""Construct harness settings and hook variables without reading the host."""

import dataclasses

import pytest
from agent_example import LINUX, MACOS, paths, settings
from test_alpha_agent_request import BUILD

from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.model import (
    ClaudePolicy,
    CodexPolicy,
    Harness,
    Host,
    Invalid,
    Profile,
    WebSearch,
)
from tbhbdev.alpha.agent.run_host import environment


@pytest.mark.parametrize(("host", "lang"), [(LINUX, "C.UTF-8"), (MACOS, "en_US.UTF-8")])
def test_codex_environment_and_hook_overrides(host: Host, lang: str) -> None:
    value = request.parse(BUILD | {"unsandboxed": True})
    files = dataclasses.replace(paths("agent"), signing_socket="/example/sign.sock")
    config = settings()
    config = dataclasses.replace(
        config,
        codex=dataclasses.replace(config.codex, web_search_build=WebSearch.CACHED),
    )
    policy = environment.build(config, host, value, files, "/example/bin")
    assert policy.profile is Profile.BUILD
    assert isinstance(policy.detail, CodexPolicy)
    assert policy.detail.web_search is WebSearch.CACHED
    assert policy.env["HOME"] == files.home
    assert policy.env["PATH"] == "/example/bin"
    assert policy.env["LANG"] == lang
    assert policy.env["CODEX_HOME"] == files.codex_home
    assert policy.env["GH_CONFIG_DIR"] == f"{files.tmpdir}/gh"
    assert policy.env["SSH_AUTH_SOCK"] == files.signing_socket
    assert policy.env["GIT_CONFIG_COUNT"] == "7"
    hooks = environment.hook_environment(policy)
    assert hooks["PATH"] == "/example/bin"
    assert not [key for key in hooks if key.startswith("GIT_")]


def test_claude_guard_uses_the_supplied_command() -> None:
    host = dataclasses.replace(LINUX, tbhbdev="/example/tools with spaces/tbhbdev")
    value = request.parse(
        BUILD
        | {
            "harness": Harness.CLAUDE.value,
            "claude_profile": "primary",
            "reason": "task",
            "unsandboxed": True,
        }
    )
    policy = environment.build(settings(), host, value, paths("agent"), "/example/bin")
    assert isinstance(policy.detail, ClaudePolicy)
    assert environment.hook_environment(policy) == policy.env
    assert policy.detail.settings["hooks"] == {
        "PreToolUse": [
            {
                "matcher": "Agent",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "'/example/tools with spaces/tbhbdev' "
                            "alpha guard-agent-model"
                        ),
                        "onFailure": "block",
                    }
                ],
            }
        ]
    }


def test_policy_refuses_an_account_the_host_does_not_enable() -> None:
    value = request.parse(BUILD | {"account": "other"})
    with pytest.raises(Invalid, match="isn't one of the accounts"):
        environment.build(settings(), LINUX, value, paths("other"), "/example/bin")
