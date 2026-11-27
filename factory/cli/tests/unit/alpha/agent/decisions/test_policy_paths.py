"""Run policy grants, denied paths, and protected paths."""

# ruff: noqa: S108 -- the paths are test data under /tmp that no test creates

from __future__ import annotations

import dataclasses
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Literal

import pytest
from agent_example import (
    DENIED,
    LINUX,
    LINUX_SOCKET_DENIES,
    MACOS_TOKEN_PATHS,
    SANDBOX,
    TOKEN_DIR,
    TOKEN_TOP,
    denied,
    paths,
    settings,
)
from policy_example import (
    COMBINATIONS,
    HOSTS,
    IDS,
    assert_build_write_roots,
    claude_settings_of,
    dig,
    expected_git_paths,
    made,
    strings,
)

from tbhbdev import secrets
from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import (
    Harness,
    Invalid,
    Os,
    Profile,
)


def assert_claude_denials(
    result: model.Policy, os: Os, profile: Profile, account: str
) -> None:
    home = f"/example/home/{account}"
    claude = claude_settings_of(result)
    fs = ("sandbox", "filesystem")
    assert dig(claude, *fs, "denyRead") == list(denied(os, home))
    worktree = paths(account).worktree
    git_paths = expected_git_paths(worktree)
    assert dig(claude, *fs, "denyWrite") == [
        *(() if profile is Profile.BUILD else (worktree,)),
        *git_paths,
        *denied(os, home),
    ]
    rules = strings(dig(claude, "permissions", "deny"))
    reads = [r for r in rules if r.startswith("Read(")]
    assert reads == [f"Read(/{p}/**)" for p in denied(os, home)]
    edits = [r for r in rules if r.startswith("Edit(")]
    expected_edits = [f"Edit(/{p}/**)" for p in denied(os, home)]
    protected = (worktree, *git_paths) if profile is Profile.REVIEW else git_paths
    expected_edits += [
        f"Edit(/{path}{suffix})" for path in protected for suffix in ("", "/**")
    ]
    assert edits == expected_edits


@pytest.mark.parametrize(("os", "profile", "harness", "account"), COMBINATIONS, ids=IDS)
def test_every_output_denies_the_same_paths(
    os: Os, profile: Profile, harness: Harness, account: str
) -> None:
    home = f"/example/home/{account}"
    result = made(os, profile, harness, account)
    assert result.deny == denied(os, home)
    assert (result.harness, result.profile) == (harness, profile)
    if isinstance(result.detail, model.CodexPolicy):
        assert result.detail.deny == denied(os, home)
    elif isinstance(result.detail, model.ClaudePolicy):
        assert_claude_denials(result, os, profile, account)
    else:
        assert not any(d in g for d in denied(os, home) for g in result.detail.grants)


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
def test_write_roots_keep_the_first_occurrence(os: Os, profile: Profile) -> None:
    run = paths("agent")
    sandbox = dataclasses.replace(
        SANDBOX,
        write_roots=(
            ".local/state/example-runs/run1.tmp",
            ".cache",
            ".local/state/example-runs/run1.tmp",
            ".cache",
        ),
    )
    result = policy.build(
        settings(sandbox=sandbox),
        HOSTS[os],
        harness=Harness.CODEX,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(run, audit_dir=run.tmpdir),
    )
    assert isinstance(result.detail, model.CodexPolicy)
    expected = (
        (run.worktree, f"{run.worktree}/.git", run.tmpdir, f"{run.home}/.cache")
        if profile is Profile.BUILD
        else (run.tmpdir,)
    )
    assert result.detail.write_roots == (
        *expected,
        *(("/dev/fd",) if os is Os.MACOS else ()),
    )


@pytest.mark.parametrize("os", Os)
def test_denied_paths_drop_children_of_denied_parents(os: Os) -> None:
    parent = ".config/example-parent"
    sandbox = dataclasses.replace(
        SANDBOX, denied_home_paths=(f"{parent}/nested/deeper", parent, DENIED)
    )
    config = settings(sandbox=sandbox)
    home = "/example/home/agent"
    denied = policy.denied_paths(config, HOSTS[os], home, 123)
    assert f"{home}/{parent}" in denied
    assert f"{home}/{DENIED}" in denied
    assert f"{home}/{parent}/nested/deeper" not in denied
    assert (f"{home}/{TOKEN_DIR}" in denied) is (os is Os.LINUX)


def test_denied_paths_hide_the_token_directory_under_a_denied_parent() -> None:
    home = "/example/home/agent"
    parent = str(PurePosixPath(TOKEN_DIR).parent)
    sandbox = dataclasses.replace(SANDBOX, denied_home_paths=(parent,))
    denied = policy.denied_paths(settings(sandbox=sandbox), HOSTS[Os.LINUX], home, 123)
    assert f"{home}/{parent}" in denied
    assert f"{home}/{TOKEN_DIR}" not in denied


def test_denied_paths_keep_a_sibling_with_a_shared_name_prefix() -> None:
    sandbox = dataclasses.replace(SANDBOX, denied_home_paths=(".cfg", ".cfg-other"))
    denied = policy.denied_paths(
        settings(sandbox=sandbox), HOSTS[Os.LINUX], "/example/home/agent", 123
    )
    assert "/example/home/agent/.cfg" in denied
    assert "/example/home/agent/.cfg-other" in denied


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
def test_denied_paths_keep_the_first_occurrence(os: Os, profile: Profile) -> None:
    sandbox = dataclasses.replace(
        SANDBOX, denied_home_paths=(DENIED, TOKEN_DIR, DENIED)
    )
    config = dataclasses.replace(
        settings(sandbox=sandbox), sockets_dir=f"/example/home/agent/{DENIED}"
    )
    result = made(os, profile, Harness.CODEX, "agent", config=config)
    home = "/example/home/agent"
    expected = (
        (
            f"{home}/{TOKEN_DIR}",
            *LINUX_SOCKET_DENIES,
            f"{home}/{DENIED}",
        )
        if os is Os.LINUX
        else (
            *(f"{home}/{p}" for p in MACOS_TOKEN_PATHS),
            f"{home}/{DENIED}",
            f"{home}/{TOKEN_DIR}",
        )
    )
    assert result.deny == expected
    assert isinstance(result.detail, model.CodexPolicy)
    assert result.detail.deny == expected


@pytest.mark.parametrize(
    ("os", "grant", "denied_path"),
    [
        (Os.LINUX, TOKEN_TOP, "{home}/" + TOKEN_DIR),
        (Os.LINUX, TOKEN_DIR, "{home}/" + TOKEN_DIR),
        (Os.LINUX, TOKEN_DIR + "/inner", "{home}/" + TOKEN_DIR),
        (Os.LINUX, ".", "{home}/" + TOKEN_DIR),
        (Os.MACOS, ".", "{home}/Library/Keychains/tbhbdev.keychain-db"),
        (Os.MACOS, DENIED, "{home}/" + DENIED),
        (Os.MACOS, DENIED + "/inner", "{home}/" + DENIED),
        (Os.MACOS, "/private/tmp", "/private/tmp/example-sockets"),
        (Os.MACOS, "/tmp/example-sockets/inner", "/tmp/example-sockets"),
    ],
)
@pytest.mark.parametrize("profile", Profile)
def test_an_agy_read_grant_that_reaches_a_denied_path_is_refused(
    os: Os, profile: Profile, grant: str, denied_path: str
) -> None:
    agy = dataclasses.replace(SANDBOX.agy, review_read=(grant,), build_read=(grant,))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    home = "/example/home/agent"
    shown = (
        grant
        if grant.startswith("/")
        else (home if grant == "." else f"{home}/{grant}")
    )
    with pytest.raises(Invalid) as caught:
        made(os, profile, Harness.AGY, "agent", config=config)
    assert str(caught.value) == (
        f"policy agy grant {shown} overlaps the denied path "
        f"{denied_path.format(home=home)}"
    )


@pytest.mark.parametrize("sibling", [DENIED + "x", DENIED + "-other", TOKEN_DIR + "2"])
def test_a_grant_beside_a_denied_path_is_allowed(sibling: str) -> None:
    agy = model.AgySandbox((sibling,), (sibling,), (sibling,))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    for profile in Profile:
        result = made(Os.LINUX, profile, Harness.AGY, "agent", config=config)
        assert isinstance(result.detail, model.AgyPolicy)
        assert f"/example/home/agent/{sibling})" in "".join(result.detail.grants)


def test_the_token_store_is_the_directory_secrets_names() -> None:
    assert not secrets.LINUX_TOKEN_FILE.parent.is_absolute()
    result = made(Os.LINUX, Profile.BUILD, Harness.CODEX, "agent")
    assert f"/example/home/agent/{TOKEN_DIR}" in result.deny


@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize(
    "grant",
    [
        "Library",
        "Library/Keychains/tbhbdev.keychain-db",
        "Library/Keychains/tbhbdev.keychain",
    ],
)
def test_agy_refuses_grants_reaching_the_macos_keychain(
    profile: Profile, grant: str
) -> None:
    agy = model.AgySandbox((grant,), (grant,), (grant,))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    with pytest.raises(Invalid, match="overlaps the denied path"):
        made(Os.MACOS, profile, Harness.AGY, "agent", config=config)


@pytest.mark.parametrize(
    ("sockets", "os", "expected"),
    [
        ("/tmp", Os.MACOS, ("/tmp", "/private/tmp")),
        ("/tmp/a/b", Os.MACOS, ("/tmp/a/b", "/private/tmp/a/b")),
        ("/tmp/a", Os.LINUX, ("/tmp/a",)),
        ("/tmp-x/a", Os.MACOS, ("/tmp-x/a",)),
        ("/var/tmp/a", Os.MACOS, ("/var/tmp/a",)),
        (".local/run/sockets", Os.MACOS, ("/example/home/agent/.local/run/sockets",)),
        ("/example/sockets", Os.LINUX, ("/example/sockets",)),
    ],
)
def test_the_sockets_directory_is_denied_with_its_aliases(
    sockets: str, os: Os, expected: tuple[str, ...]
) -> None:
    config = dataclasses.replace(settings(), sockets_dir=sockets)
    result = made(os, Profile.REVIEW, Harness.CODEX, "agent", config=config)
    home = "/example/home/agent"
    assert result.deny == (
        *(
            (
                f"{home}/{TOKEN_DIR}",
                "/run/WSL",
                "/var/run/docker.sock",
                "/run/docker.sock",
                "/run/user/0",
            )
            if os is Os.LINUX
            else tuple(f"{home}/{p}" for p in MACOS_TOKEN_PATHS)
        ),
        *expected,
        f"{home}/{DENIED}",
    )


def test_denied_home_paths_each_resolve_against_the_home() -> None:
    sandbox = dataclasses.replace(
        SANDBOX,
        denied_home_paths=(DENIED, ".local/state/x", "/tmp/y"),
        write_roots=(".cache",),
    )
    result = made(
        Os.MACOS, Profile.BUILD, Harness.CODEX, "ops", config=settings(sandbox=sandbox)
    )
    home = "/example/home/ops"
    assert result.deny == (
        *(f"{home}/{p}" for p in MACOS_TOKEN_PATHS),
        "/tmp/example-sockets",
        "/private/tmp/example-sockets",
        f"{home}/{DENIED}",
        f"{home}/.local/state/x",
        "/tmp/y",
        "/private/tmp/y",
    )


@pytest.mark.parametrize(
    ("home", "path", "expected"),
    [
        ("/h", "a/b", "/h/a/b"),
        ("/h", "/abs/a", "/abs/a"),
        ("/h/", "a", "/h/a"),
    ],
)
def test_resolve(home: str, path: str, expected: str) -> None:
    assert policy.resolve(home, path) == expected


def test_write_roots_come_only_from_the_config() -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(".only-this", "/example/abs"))
    config = settings(sandbox=sandbox)
    result = made(Os.LINUX, Profile.BUILD, Harness.CODEX, config=config)
    assert isinstance(result.detail, model.CodexPolicy)
    home = "/example/home/agent"
    assert result.detail.write_roots == (
        f"{home}/Code/example.com/example-org/example-repo",
        f"{home}/Code/example.com/example-org/example-repo/.git",
        f"{home}/.local/state/example-runs/run1.tmp",
        f"{home}/.only-this",
        "/example/abs",
    )


def test_a_review_ignores_the_build_write_roots() -> None:
    result = made(Os.MACOS, Profile.REVIEW, Harness.CLAUDE, "agent")
    assert dig(claude_settings_of(result), "sandbox", "filesystem", "allowWrite") == [
        "/example/home/agent/.local/state/example-runs/run1.tmp",
        "/example/home/agent/.local/state/example-audit",
        "/dev/fd",
    ]


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("entry", ["/example/host/temp", "/example/host/cache"])
@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_absolute_write_roots_pass_through(
    os: Os, entry: str, harness: Harness
) -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(entry,))
    result = made(os, Profile.BUILD, harness, "agent", config=settings(sandbox=sandbox))
    assert_build_write_roots(
        result,
        os,
        [
            "/example/home/agent/Code/example.com/example-org/example-repo",
            "/example/home/agent/.local/state/example-runs/run1.tmp",
            entry,
        ],
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize(
    ("os", "entry", "shown", "denied_path"),
    [
        (
            Os.LINUX,
            ".config/tbhbdev/secrets",
            ".config/tbhbdev/secrets",
            ".config/tbhbdev/secrets",
        ),
        (
            Os.LINUX,
            ".config",
            ".config",
            ".config/tbhbdev/secrets",
        ),
        (
            Os.LINUX,
            ".config/tbhbdev/secrets/inner",
            ".config/tbhbdev/secrets/inner",
            ".config/tbhbdev/secrets",
        ),
        (
            Os.LINUX,
            ".",
            "/example/home/agent",
            ".config/tbhbdev/secrets",
        ),
        (
            Os.MACOS,
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
        ),
        (
            Os.MACOS,
            "Library/Keychains",
            "/example/home/agent/Library/Keychains",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
        ),
        (
            Os.MACOS,
            "Library/Keychains/tbhbdev.keychain-db/inner",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db/inner",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
        ),
        (
            Os.MACOS,
            ".",
            "/example/home/agent",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
        ),
        (
            Os.MACOS,
            "Library/Keychains/tbhbdev.keychain",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain",
        ),
        (Os.MACOS, "/private/tmp", "/private/tmp", "/private/tmp/example-sockets"),
        (Os.LINUX, "/tmp", "/tmp", "/tmp/example-sockets"),
    ],
)
def test_configured_write_roots_cannot_reach_denied_paths(
    os: Os, harness: Harness, entry: str, shown: str, denied_path: str
) -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(entry,))
    with pytest.raises(Invalid) as caught:
        made(os, Profile.BUILD, harness, "agent", config=settings(sandbox=sandbox))
    home = "/example/home/agent"
    shown = shown if shown.startswith("/") else f"{home}/{shown}"
    denied_path = (
        denied_path if denied_path.startswith("/") else f"{home}/{denied_path}"
    )
    expected = (
        f"protected host path {paths('agent').worktree}/.git/hooks "
        f"overlaps write root {shown}; narrow that write root"
        if entry == "."
        else f"policy write path {shown} overlaps the denied path {denied_path}"
    )
    assert str(caught.value) == expected


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize(
    ("profile", "field"),
    [
        (Profile.BUILD, "worktree"),
        (Profile.BUILD, "tmpdir"),
        (Profile.REVIEW, "tmpdir"),
        (Profile.REVIEW, "audit_dir"),
    ],
)
@pytest.mark.parametrize(
    "write", ["/example/denied/item", "/example/denied", "/example/denied/item/inner"]
)
def test_run_write_paths_cannot_reach_denied_paths(
    os: Os,
    profile: Profile,
    harness: Harness,
    field: Literal["worktree", "tmpdir", "git_common_dir", "audit_dir"],
    write: str,
) -> None:
    original = paths("agent")
    run = dataclasses.replace(
        original,
        worktree=write if field == "worktree" else original.worktree,
        tmpdir=write if field == "tmpdir" else original.tmpdir,
        git_common_dir=write if field == "git_common_dir" else original.git_common_dir,
        audit_dir=write if field == "audit_dir" else original.audit_dir,
    )
    config = dataclasses.replace(settings(), sockets_dir="/example/denied/item")
    with pytest.raises(Invalid) as caught:
        policy.build(
            config,
            HOSTS[os],
            harness=harness,
            profile=profile,
            account="agent",
            unsandboxed=False,
            paths=run,
        )
    assert str(caught.value) == (
        f"policy write path {write} overlaps the denied path /example/denied/item"
    )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("write", [".example-denied", ".example-denied/inner"])
def test_agy_configured_write_grants_cannot_reach_denied_paths(
    os: Os, write: str
) -> None:
    agy = dataclasses.replace(SANDBOX.agy, build_write=(write,))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    with pytest.raises(Invalid) as caught:
        made(os, Profile.BUILD, Harness.AGY, "agent", config=config)
    assert str(caught.value) == (
        f"policy write path /example/home/agent/{write} overlaps the denied path "
        "/example/home/agent/.example-denied"
    )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize(
    "write", ["/example/host/cache", "/example/home/agent/.example-denied-other"]
)
def test_unrelated_write_roots_are_allowed(
    os: Os, harness: Harness, profile: Profile, write: str
) -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(write,))
    assert made(os, profile, harness, "agent", config=settings(sandbox=sandbox))


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize(
    ("socket", "denied_path"),
    [
        ("/example/denied/item", "/example/denied/item"),
        ("/example/denied/item/signing.sock", "/example/denied/item"),
        ("/example/denied", "/example/denied/item"),
        ("/tmp/example-sockets/signing.sock", "/tmp/example-sockets"),
    ],
)
def test_signing_sockets_cannot_reach_denied_paths(
    os: Os, harness: Harness, profile: Profile, socket: str, denied_path: str
) -> None:
    sandbox = dataclasses.replace(SANDBOX, denied_home_paths=("/example/denied/item",))
    with pytest.raises(Invalid) as caught:
        policy.build(
            settings(sandbox=sandbox),
            HOSTS[os],
            harness=harness,
            profile=profile,
            account="agent",
            unsandboxed=False,
            paths=dataclasses.replace(paths("agent"), signing_socket=socket),
        )
    assert str(caught.value) == (
        f"policy signing socket {socket} overlaps the denied path {denied_path}"
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_signing_socket_grants_do_not_add_write_roots(
    os: Os, harness: Harness, profile: Profile
) -> None:
    socket = "/example/signing.sock"
    original = made(os, profile, harness, "agent")
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=harness,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), signing_socket=socket),
    )
    assert result.env == original.env | {"SSH_AUTH_SOCK": socket}
    if isinstance(result.detail, model.CodexPolicy):
        assert result.detail == original.detail
    elif isinstance(result.detail, model.ClaudePolicy):
        assert dig(result.detail.settings, "sandbox", "filesystem") == dig(
            claude_settings_of(original), "sandbox", "filesystem"
        )
    else:
        assert isinstance(original.detail, model.AgyPolicy)
        reads = tuple(g for g in original.detail.grants if g.startswith("read_file("))
        writes = tuple(g for g in original.detail.grants if g.startswith("write_file("))
        assert result.detail.grants == (*reads, f"read_file({socket})", *writes)


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("common", [None, "/example/clone/.git"])
def test_codex_protects_git_configuration_inside_build_write_root(
    os: Os, profile: Profile, common: str | None
) -> None:
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=Harness.CODEX,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), git_common_dir=common),
    )
    assert isinstance(result.detail, model.CodexPolicy)
    expected = (":root",)
    if profile is Profile.BUILD:
        expected += expected_git_paths(paths("agent").worktree)[1:]
    assert common not in result.detail.write_roots
    assert result.detail.read_roots == expected


@pytest.mark.parametrize(
    "field", ["worktree", "tmpdir", "audit_dir", "configured", "fd"]
)
@pytest.mark.parametrize("relation", ["same", "ancestor", "descendant", "sibling"])
def test_host_protected_paths_refuse_both_directions(field: str, relation: str) -> None:
    run = model.RunPaths(
        "/example/home",
        "/one/worktree",
        "/three/temp",
        "/three/temp/codex",
        "/four/audit",
        "/two/common",
    )
    configured = settings(sandbox=dataclasses.replace(SANDBOX, write_roots=(".cache",)))
    write = {"configured": run.home + "/.cache", "fd": "/dev/fd"}.get(field) or getattr(
        run, field
    )
    protected = {
        "same": write,
        "ancestor": str(PurePosixPath(write).parent),
        "descendant": write + "/bin",
        "sibling": write + "-other",
    }[relation]
    if relation == "sibling":
        policy.protect_host(configured, run, (protected,))
    else:
        with pytest.raises(Invalid) as caught:
            policy.protect_host(configured, run, (protected,))
        assert str(caught.value) == (
            f"protected host path {protected} overlaps write root {write}; "
            "narrow that write root"
        )


@pytest.mark.parametrize("harness", Harness)
def test_linux_socket_denies_use_the_run_uid(harness: Harness) -> None:
    run = dataclasses.replace(paths("agent"), uid=12345)
    result = made(Os.LINUX, Profile.BUILD, harness, run=run)
    assert "/run/user/12345" in result.deny
    assert "/run/user/0" not in result.deny
    for path in ("/run/WSL", "/var/run/docker.sock", "/run/docker.sock"):
        assert path in result.deny


def test_host_protection_handles_a_missing_common_directory() -> None:
    policy.protect_host(settings(), paths("agent"), ("/example/installation",))


def test_git_entry_and_all_configuration_paths_are_protected() -> None:
    run = paths("agent")
    assert policy.protected_git(run) == expected_git_paths(run.worktree)


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("denied", [(), ("/example/protected",)])
def test_supplied_denies_are_policy_data(
    harness: Harness, denied: tuple[str, ...]
) -> None:
    value = policy.build(
        settings(),
        LINUX,
        harness=harness,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=paths("agent"),
        denied=denied,
    )
    assert value.deny == denied
    if isinstance(value.detail, model.CodexPolicy):
        assert value.detail.deny == denied
    elif isinstance(value.detail, model.ClaudePolicy):
        assert dig(value.detail.settings, "sandbox", "filesystem", "denyRead") == list(
            denied
        )


@pytest.mark.parametrize("os", Os)
def test_signing_file_is_denied_with_aliases(os: Os) -> None:
    original = settings()
    config = dataclasses.replace(
        original,
        credentials=tuple(
            dataclasses.replace(c, signing="/tmp/example-key")
            for c in original.credentials
        ),
    )
    denied = policy.denied_paths(config, HOSTS[os], paths("agent").home, 123)
    assert "/tmp/example-key" in denied
    assert ("/private/tmp/example-key" in denied) is (os is Os.MACOS)


@pytest.mark.parametrize("field", ["tmpdir", "audit_dir", "configured", "agy"])
@pytest.mark.parametrize("suffix", ["", "/nested", "/../"])
def test_hooks_directory_refuses_extra_write_grants(field: str, suffix: str) -> None:
    run = paths("agent")
    hooks = run.worktree + "/.git/hooks"
    write = str(PurePosixPath(hooks).parent) if suffix == "/../" else hooks + suffix
    configured = settings()
    if field == "configured":
        configured = dataclasses.replace(
            configured, sandbox=dataclasses.replace(SANDBOX, write_roots=(write,))
        )
    elif field == "agy":
        configured = dataclasses.replace(
            configured,
            sandbox=dataclasses.replace(
                SANDBOX, agy=dataclasses.replace(SANDBOX.agy, build_write=(write,))
            ),
        )
    else:
        run = (
            dataclasses.replace(run, tmpdir=write)
            if field == "tmpdir"
            else dataclasses.replace(run, audit_dir=write)
        )
    with pytest.raises(Invalid, match="hooks"):
        policy.protect_host(configured, run, ())
