"""Run policy behavior shared by the harnesses."""

from __future__ import annotations

import dataclasses
import itertools

import pytest
from agent_example import (
    LINUX,
    MARKER,
    SANDBOX,
    paths,
    settings,
)
from policy_example import (
    ACCOUNTS,
    HOSTS,
    dig,
    env_of,
    guard_hooks,
    made,
    strings,
)

from tbhbdev import secrets
from tbhbdev.alpha.agent import host_tools, model
from tbhbdev.alpha.agent.decisions import policy, policy_agy
from tbhbdev.alpha.agent.model import (
    Harness,
    Invalid,
    Os,
    Profile,
)


def test_a_host_that_allows_it_still_sandboxes_unless_asked() -> None:
    result = made(Os.LINUX, Profile.BUILD, Harness.CODEX, "agent")
    assert isinstance(result.detail, model.CodexPolicy)
    assert result.detail.mode is model.CodexMode.PROFILE


@pytest.mark.parametrize("unsandboxed", [False, True])
def test_an_account_the_host_lacks_is_refused(unsandboxed: bool) -> None:
    with pytest.raises(Invalid) as caught:
        policy.build(
            settings(),
            dataclasses.replace(LINUX, accounts=("agent",)),
            harness=Harness.CODEX,
            profile=Profile.BUILD,
            account="ops",
            unsandboxed=unsandboxed,
            paths=paths("ops"),
        )
    assert (
        str(caught.value)
        == "policy account 'ops' isn't one of the accounts of example-linux"
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("account", ACCOUNTS)
def test_macos_denies_the_keychain_file_and_alias(
    harness: Harness, profile: Profile, account: str
) -> None:
    assert secrets.MACOS_KEYCHAIN_FILE.as_posix() == (
        "Library/Keychains/tbhbdev.keychain-db"
    )
    assert not secrets.MACOS_KEYCHAIN_FILE.is_absolute()
    result = made(Os.MACOS, profile, harness, account)
    assert result.deny[:2] == (
        f"/example/home/{account}/Library/Keychains/tbhbdev.keychain-db",
        f"/example/home/{account}/Library/Keychains/tbhbdev.keychain",
    )


def test_no_output_holds_a_secret_reference() -> None:
    config = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")
    for os, profile, harness in itertools.product(Os, Profile, Harness):
        result = made(os, profile, harness, "agent", config=config)
        assert MARKER not in repr(result)


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize(
    ("harness", "profile"),
    [
        (Harness.CODEX, Profile.BUILD),
        (Harness.CLAUDE, Profile.BUILD),
        (Harness.CODEX, Profile.REVIEW),
        (Harness.CLAUDE, Profile.REVIEW),
        (Harness.AGY, Profile.REVIEW),
    ],
)
def test_unused_agy_build_write_grants_do_not_block_other_policies(
    os: Os, harness: Harness, profile: Profile
) -> None:
    agy = dataclasses.replace(SANDBOX.agy, build_write=(".example-denied",))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    assert made(os, profile, harness, "agent", config=config)


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("denied_path", ["/dev", "/dev/fd", "/dev/fd/inner"])
def test_dev_fd_cannot_reach_a_denied_path(
    harness: Harness, profile: Profile, denied_path: str
) -> None:
    config = dataclasses.replace(settings(), sockets_dir=denied_path)
    with pytest.raises(Invalid) as caught:
        made(Os.MACOS, profile, harness, "agent", config=config)
    assert str(caught.value) == (
        f"policy write path /dev/fd overlaps the denied path {denied_path}"
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_unsandboxed_skips_all_sandbox_checks(
    harness: Harness, profile: Profile, os: Os, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = dataclasses.replace(
        SANDBOX,
        write_roots=(".example-denied",),
        agy=model.AgySandbox(
            (".example-denied",), (".example-denied",), (".example-denied",)
        ),
    )
    run_paths = dataclasses.replace(paths("agent"), signing_socket="/example/denied")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("full access must skip sandbox policy checks")

    for name in (
        "protect_host",
        "denied_paths",
        "tool_path",
        "refuse_overlaps",
    ):
        monkeypatch.setattr(policy, name, unexpected)
    monkeypatch.setattr(policy_agy, "build", unexpected)
    value = policy.build(
        settings(sandbox=sandbox),
        dataclasses.replace(HOSTS[os], unsandboxed=False),
        harness=harness,
        profile=profile,
        account="agent",
        unsandboxed=True,
        paths=run_paths,
        protected=(run_paths.worktree,),
        denied=("/example/denied",),
    )
    assert value.deny == ()
    assert value.tool_path == ":".join(host_tools.account_path(run_paths.home))
    assert value.env["PATH"] == value.tool_path
    assert value.env["SSH_AUTH_SOCK"] == "/example/denied"
    if isinstance(value.detail, model.ClaudePolicy):
        assert value.detail.settings == {
            "env": {
                "GH_CONFIG_DIR": f"{run_paths.tmpdir}/gh",
                "DOCKER_CONFIG": f"{run_paths.tmpdir}/docker",
                "CLAUDE_CODE_TMPDIR": run_paths.tmpdir,
                "CLAUDE_CODE_FORK_SUBAGENT": "0",
            },
            "hooks": guard_hooks(f"{run_paths.home}/.local/bin/tbhbdev"),
            "sandbox": {"enabled": False},
        }
    elif isinstance(value.detail, model.AgyPolicy):
        assert value.detail.grants == ()


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
def test_unsandboxed_policy_for_every_harness(
    harness: Harness, profile: Profile
) -> None:
    value = made(Os.LINUX, profile, harness, "agent", unsandboxed=True)
    if isinstance(value.detail, model.ClaudePolicy):
        assert value.detail.settings["sandbox"] == {"enabled": False}
    elif isinstance(value.detail, model.CodexPolicy):
        assert value.detail.mode is model.CodexMode.FULL_ACCESS
        assert value.detail.network_proxy is None
    else:
        assert isinstance(value.detail, model.AgyPolicy)


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize(
    "common",
    [
        None,
        "/example/clone/.git",
        "/example/home/agent/.local/state/example-runs/run1.tmp",
    ],
)
def test_build_never_grants_another_git_directory(
    os: Os, harness: Harness, common: str | None
) -> None:
    run = dataclasses.replace(
        paths("agent"),
        worktree="/example/clone/.worktrees/agent-example",
        git_common_dir=common,
    )
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=harness,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=run,
    )
    if isinstance(result.detail, model.AgyPolicy):
        assert result.detail.grants == (
            "read_file(/example/home/agent/.local/share/example-tools)",
            "read_file(/example/home/agent/.local/state/example-runs/run1.tmp/gh)",
            "read_file(/example/home/agent/.local/state/example-runs/run1.tmp/docker)",
            "write_file(/example/clone/.worktrees/agent-example)",
            "write_file(/example/home/agent/.local/state/example-runs/run1.tmp)",
            "write_file(/example/home/agent/.local/state/mise)",
            "write_file(/example/home/agent/.local/share/mise)",
        )
    else:
        expected = ["/example/clone/.worktrees/agent-example"]
        expected += [
            "/example/home/agent/.local/state/example-runs/run1.tmp",
            "/example/home/agent/.cache",
            "/example/home/agent/.local/share/mise",
            "/example/home/agent/.local/state",
        ]
        if os is Os.MACOS:
            expected.append("/dev/fd")
        if isinstance(result.detail, model.CodexPolicy):
            expected.insert(1, expected[0] + "/.git")
            assert result.detail.write_roots == tuple(expected)
        else:
            assert (
                dig(result.detail.settings, "sandbox", "filesystem", "allowWrite")
                == expected
            )


@pytest.mark.parametrize("harness", Harness)
def test_build_gets_common_git_directory_and_only_its_signing_socket(
    harness: Harness,
) -> None:
    original = paths("agent")
    common = "/example/clone/git-common"
    socket = original.tmpdir + "/ssh.sock"
    run = dataclasses.replace(original, git_common_dir=common, signing_socket=socket)
    result = policy.build(
        settings(),
        LINUX,
        harness=harness,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=run,
    )
    assert result.env == env_of(Os.LINUX, harness, "agent") | {"SSH_AUTH_SOCK": socket}
    if isinstance(result.detail, model.CodexPolicy):
        assert result.detail.write_roots == (
            original.worktree,
            original.worktree + "/.git",
            original.tmpdir,
            original.home + "/.cache",
            original.home + "/.local/share/mise",
            original.home + "/.local/state",
        )
    elif isinstance(result.detail, model.ClaudePolicy):
        assert dig(result.detail.settings, "env", "SSH_AUTH_SOCK") == socket
        assert dig(result.detail.settings, "sandbox", "network") == {
            "allowAllUnixSockets": False,
            "allowLocalBinding": False,
            "strictAllowlist": True,
            "allowedDomains": [],
            "allowUnixSockets": [socket],
        }
        assert common not in strings(
            dig(result.detail.settings, "sandbox", "filesystem", "allowWrite")
        )
    else:
        assert f"read_file({socket})" in result.detail.grants


def test_review_never_grants_common_git_writes() -> None:
    run = dataclasses.replace(
        paths("agent"), git_common_dir="/example/clone/git-common"
    )
    result = policy.build(
        settings(),
        LINUX,
        harness=Harness.CODEX,
        profile=Profile.REVIEW,
        account="agent",
        unsandboxed=False,
        paths=run,
    )
    assert isinstance(result.detail, model.CodexPolicy)
    assert "/example/clone/git-common" not in result.detail.write_roots
