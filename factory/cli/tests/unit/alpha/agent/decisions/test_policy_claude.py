"""Claude run permissions, sandbox settings, and launch guard."""

from __future__ import annotations

import dataclasses

import pytest
from agent_example import (
    LINUX,
    SANDBOX,
    denied,
    paths,
    settings,
)
from policy_example import (
    ACCOUNTS,
    HOSTS,
    PATH,
    claude_settings_of,
    dig,
    expected_git_paths,
    guard_hooks,
    made,
    sandbox_environment,
    strings,
)

from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import (
    Harness,
    Os,
    Profile,
)


def claude_file_tool_denies(
    profile: Profile, worktree: str, protected: list[str]
) -> list[str]:
    if profile is Profile.REVIEW:
        protected = [worktree, *protected]
    return [f"Edit(/{path}{suffix})" for path in protected for suffix in ("", "/**")]


def claude_expected(os: Os, profile: Profile, account: str) -> dict[str, object]:
    home = f"/example/home/{account}"
    tmp = f"{home}/.local/state/example-runs/run1.tmp"
    worktree = f"{home}/Code/example.com/example-org/example-repo"
    deny = list(denied(os, home))
    denied_tools = [f"{tool}(/{p}/**)" for p in deny for tool in ("Read", "Edit")]
    if profile is Profile.BUILD:
        writes = [
            worktree,
            tmp,
            f"{home}/.cache",
            f"{home}/.local/share/mise",
            f"{home}/.local/state",
        ]
        if os is Os.MACOS:
            writes.append("/dev/fd")
        allow = [f"Edit(/{p}/**)" for p in writes]
        rules = denied_tools
        deny_write = deny
    else:
        writes = [tmp, f"{home}/.local/state/example-audit"]
        if os is Os.MACOS:
            writes.append("/dev/fd")
        allow: list[str] = []
        rules = [
            "Edit",
            "Write",
            "NotebookEdit",
            *denied_tools,
        ]
        deny_write = [worktree, *deny]
    protected = list(expected_git_paths(worktree))
    rules += claude_file_tool_denies(profile, worktree, protected)
    deny_write = (
        [*protected, *deny_write]
        if profile is Profile.BUILD
        else [worktree, *protected, *deny]
    )
    return {
        "env": {
            "PATH": PATH[os].format(home=home),
            **sandbox_environment(tmp),
            "CLAUDE_CODE_FORK_SUBAGENT": "0",
        },
        "hooks": guard_hooks(f"{home}/.local/bin/tbhbdev"),
        "permissions": {
            "allow": allow,
            "deny": [*rules, "WebFetch", "WebSearch"],
        },
        "sandbox": {
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "enabled": True,
            "failIfUnavailable": True,
            "enableWeakerNetworkIsolation": True,
            **(
                {
                    "network": {
                        "allowAllUnixSockets": False,
                        "allowLocalBinding": False,
                        "strictAllowlist": True,
                        "allowedDomains": [
                            "github.com",
                            "api.github.com",
                            "*.githubusercontent.com",
                        ],
                    },
                }
                if profile is Profile.REVIEW
                else {
                    "network": {
                        "allowAllUnixSockets": False,
                        "allowLocalBinding": False,
                        "strictAllowlist": True,
                        "allowedDomains": [],
                    }
                }
            ),
            "filesystem": {
                "allowWrite": writes,
                "denyRead": deny,
                "denyWrite": deny_write,
            },
        },
    }


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_claude_settings(os: Os, profile: Profile, account: str) -> None:
    result = made(os, profile, Harness.CLAUDE, account)
    assert result.detail == model.ClaudePolicy(claude_expected(os, profile, account))


@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_claude_settings_emit_no_path_write_rules(os: Os, profile: Profile) -> None:
    # Claude warns that `Write(path)` rules are not matched by file permission
    # checks and ignores them; `Edit(path)` rules cover every file-writing tool.
    result = made(os, profile, Harness.CLAUDE, "agent")
    permissions = dig(claude_settings_of(result), "permissions")
    rules = [*strings(dig(permissions, "allow")), *strings(dig(permissions, "deny"))]
    assert not [rule for rule in rules if rule.startswith(("Write(", "NotebookEdit("))]


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("common", [None, "/example/clone/.git"])
def test_claude_review_denies_writes_to_the_git_common_directory(
    os: Os, profile: Profile, common: str | None
) -> None:
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=Harness.CLAUDE,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), git_common_dir=common),
    )
    expected = []
    if profile is Profile.REVIEW:
        expected.append("/example/home/agent/Code/example.com/example-org/example-repo")
    expected.extend(expected_git_paths(paths("agent").worktree))
    assert dig(claude_settings_of(result), "sandbox", "filesystem", "denyWrite") == [
        *expected,
        *denied(os, "/example/home/agent"),
    ]


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize(
    ("profile", "socket", "network"),
    [
        (
            Profile.BUILD,
            None,
            {
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
                "strictAllowlist": True,
                "allowedDomains": [],
            },
        ),
        (
            Profile.BUILD,
            "/example/signing.sock",
            {
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
                "strictAllowlist": True,
                "allowedDomains": [],
                "allowUnixSockets": ["/example/signing.sock"],
            },
        ),
        (
            Profile.REVIEW,
            None,
            {
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
                "strictAllowlist": True,
                "allowedDomains": [
                    "github.com",
                    "api.github.com",
                    "*.githubusercontent.com",
                ],
            },
        ),
        (
            Profile.REVIEW,
            "/example/signing.sock",
            {
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
                "strictAllowlist": True,
                "allowedDomains": [
                    "github.com",
                    "api.github.com",
                    "*.githubusercontent.com",
                ],
                "allowUnixSockets": ["/example/signing.sock"],
            },
        ),
    ],
)
def test_claude_combines_review_network_and_signing_socket(
    os: Os, profile: Profile, socket: str | None, network: dict[str, object] | None
) -> None:
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=Harness.CLAUDE,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), signing_socket=socket),
    )
    expected = claude_expected(os, profile, "agent")
    sandbox = expected["sandbox"]
    assert isinstance(sandbox, dict)
    if network is not None:
        sandbox["network"] = network
    env = expected["env"]
    assert isinstance(env, dict)
    if socket is not None:
        env["SSH_AUTH_SOCK"] = socket
    assert claude_settings_of(result) == expected


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("socket", [None, "/example/signing.sock"])
def test_claude_build_network_domains(os: Os, socket: str | None) -> None:
    configured = settings(
        sandbox=dataclasses.replace(SANDBOX, network_domains=("api.github.com",))
    )
    result = policy.build(
        configured,
        HOSTS[os],
        harness=Harness.CLAUDE,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), signing_socket=socket),
    )
    assert isinstance(result.detail, model.ClaudePolicy)
    assert dig(result.detail.settings, "sandbox", "network") == {
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
        "strictAllowlist": True,
        "allowedDomains": ["api.github.com"],
        **({"allowUnixSockets": [socket]} if socket else {}),
    }


@pytest.mark.parametrize("profile", Profile)
def test_claude_file_tools_deny_each_protected_path(profile: Profile) -> None:
    run = paths("agent")
    protected = ("/example/mirror", "/example/account/.git")
    value = policy.build(
        settings(),
        LINUX,
        harness=Harness.CLAUDE,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=run,
        protected=protected,
    )
    detail = claude_settings_of(value)
    denied = strings(dig(detail, "permissions", "deny"))
    for path in (
        *protected,
        *(f"{run.worktree}/.git/{name}" for name in ("config", "hooks", "info")),
    ):
        assert f"Edit(/{path})" in denied
        assert f"Edit(/{path}/**)" in denied
        assert path in strings(dig(detail, "sandbox", "filesystem", "denyWrite"))
    assert not any(
        "/.git" in rule for rule in strings(dig(detail, "permissions", "allow"))
    )


@pytest.mark.parametrize("unsandboxed", [False, True])
@pytest.mark.parametrize("profile", Profile)
def test_claude_guards_agent_launches_and_pins_no_model(
    profile: Profile, *, unsandboxed: bool
) -> None:
    """Subagents stay on, forks are off, a guard hook runs, and nothing pins a model."""
    result = made(Os.LINUX, profile, Harness.CLAUDE, "agent", unsandboxed=unsandboxed)
    value = claude_settings_of(result)
    assert "disableAllHooks" not in value
    assert value["hooks"] == guard_hooks("/example/home/agent/.local/bin/tbhbdev")
    env = dig(value, "env")
    assert isinstance(env, dict)
    assert env["CLAUDE_CODE_FORK_SUBAGENT"] == "0"
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env
    permissions = value.get("permissions", {})
    assert isinstance(permissions, dict)
    assert "Agent" not in permissions.get("deny", [])


@pytest.mark.parametrize("unsandboxed", [False, True])
@pytest.mark.parametrize("profile", Profile)
def test_claude_guard_blocks_when_it_cannot_run(
    profile: Profile, *, unsandboxed: bool
) -> None:
    """A guard that fails to start, times out or crashes must refuse the launch."""
    result = made(Os.LINUX, profile, Harness.CLAUDE, "agent", unsandboxed=unsandboxed)
    groups = dig(claude_settings_of(result), "hooks", "PreToolUse")
    assert groups == guard_hooks("/example/home/agent/.local/bin/tbhbdev")["PreToolUse"]
    assert "'onFailure': 'block'" in str(groups)


@pytest.mark.parametrize("unsandboxed", [False, True])
def test_claude_guard_runs_the_hosts_configured_binary(*, unsandboxed: bool) -> None:
    host = dataclasses.replace(LINUX, tbhbdev="/example path/bin/tbhbdev")
    result = policy.build(
        settings(),
        host,
        harness=Harness.CLAUDE,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=unsandboxed,
        paths=paths("agent"),
    )
    command = dig(claude_settings_of(result), "hooks", "PreToolUse")
    assert command == guard_hooks("'/example path/bin/tbhbdev'")["PreToolUse"]
