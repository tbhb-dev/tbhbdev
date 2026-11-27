"""Run-host validation and launch choices with literal plan."""

# gazelle:include_pytest_conftest false

from __future__ import annotations

import json
from pathlib import PurePosixPath

import pytest
from agent_example import LINUX, MACOS, settings
from test_alpha_agent_request import BUILD, CLAUDE

from tbhbdev.alpha.agent.model import (
    AgyPolicy,
    Harness,
    Invalid,
    Policy,
    Profile,
    RunKind,
)
from tbhbdev.alpha.agent.run_host import plan


def test_select_host_by_name() -> None:
    assert plan.select_host((LINUX, MACOS), MACOS.name) is MACOS
    with pytest.raises(Invalid, match="host 'unknown' is not configured"):
        plan.select_host((LINUX, MACOS), "unknown")


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ([], "request must be a JSON object"),
        (
            BUILD | {"account": "other"},
            "account 'other' is not enabled on example-linux",
        ),
        (
            CLAUDE | {"claude_profile": "unknown"},
            "claude profile 'unknown' is not configured",
        ),
    ],
)
def test_parse_request_refuses_invalid_host_inputs(data: object, error: str) -> None:
    with pytest.raises(Invalid, match=error):
        plan.parse_request(json.dumps(data), LINUX, settings().claude)


def test_parse_request_uses_the_configured_profile_spelling() -> None:
    parsed = plan.parse_request(
        json.dumps(CLAUDE | {"claude_profile": "PRIMARY"}), LINUX, settings().claude
    )
    assert parsed.claude_profile == "primary"


def test_protected_home_lists_startup_files_and_socket_ancestors() -> None:
    home = PurePosixPath("/example/home")
    zsh = (".zshenv", ".zshrc", ".zprofile", ".zlogin")
    bash = (".bashrc", ".bash_profile", ".profile")
    tools = (".tmux.conf", ".config/tmux", ".gitconfig", ".config/git", ".config/mise")
    credentials = (".ssh", ".claude", ".claude.json", ".codex", ".codex-recovery")
    startup = (*zsh, *bash, *tools, *credentials)
    assert plan.protected_home_paths(home, "/separate/sockets") == tuple(
        home / path for path in startup
    )
    assert plan.protected_home_paths(home, ".control/sockets/private") == tuple(
        home / path for path in (*startup, ".control/sockets", ".control")
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("unsandboxed", [False, True])
def test_hook_environment(harness: Harness, unsandboxed: bool) -> None:
    env = {
        "PATH": "/child-tools",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "example",
    }
    value = Policy(harness, Profile.BUILD, env, (), AgyPolicy(()), "/hook-tools")
    expected = env
    if harness is Harness.CODEX:
        expected = {"PATH": "/hook-tools", "GIT_CONFIG_COUNT": "0"}
    elif harness is Harness.CLAUDE and not unsandboxed:
        expected = env | {"PATH": "/hook-tools"}
    assert plan.hook_environment(value, unsandboxed=unsandboxed) == expected


@pytest.mark.parametrize("kind", RunKind)
@pytest.mark.parametrize("harness", Harness)
def test_finalize_command(kind: RunKind, harness: Harness) -> None:
    output = "/example/out.final"
    extra = (
        ("--output-last-message", output)
        if (kind, harness) == (RunKind.EXEC, Harness.CODEX)
        else ()
    )
    assert plan.finalize_command(
        (harness.value, "--model", "example", "-"),
        "/tools/harness",
        kind,
        harness,
        output,
    ) == ("/tools/harness", "--model", "example", *extra, "-")


def test_agy_project_uses_run_values() -> None:
    assert json.loads(
        plan.agy_project("example", "/tree", ("read_file(/tree)",), 42)
    ) == {
        "name": "agent-example",
        "path": "/tree",
        "permissions": ["read_file(/tree)"],
        "timeout": 42,
    }
