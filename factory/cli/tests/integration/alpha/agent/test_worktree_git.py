"""Host-controlled Git commands used during worktree preparation."""

from __future__ import annotations

import base64
import os
import subprocess
from typing import Unpack
lazy from pathlib import Path

import pytest
from agent_host_example import (
    HostGitOptions,
    git,
    repository,
    run_request,
)

from tbhbdev.alpha.agent import (
    host_tools,
    worktree_checkout,
    worktree_git,
)


def test_git_suppresses_command_errors_and_returns_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def output(
        command: list[str],
        **kwargs: Unpack[HostGitOptions],
    ) -> str:
        env = kwargs["env"]
        assert command[0] == host_tools.resolve("git")
        prefix = [
            host_tools.resolve("git"),
            *(item for value in worktree_git.GIT_OVERRIDES for item in ("-c", value)),
            "--git-dir",
            str(tmp_path),
            "-c",
            "protocol.allow=never",
        ]
        assert env == {
            key: os.environ[key] for key in ("HOME", "LANG") if key in os.environ
        } | {
            "PATH": host_tools.search(),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": (
                "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
            ),
        }
        assert kwargs["cwd"] == "/"
        assert kwargs["timeout"] == 30
        assert kwargs["text"]
        assert kwargs["stderr"] == subprocess.PIPE
        if command[-1] == r"^(filter|merge)\.":
            options = ["config", "--null", "--name-only", "--get-regexp"]
            assert command == [*prefix, *options, r"^(filter|merge)\."]
            return "filter.example.smudge\0merge.example.driver\0"
        assert command == [
            *prefix,
            "-c",
            "filter.example.smudge=",
            "-c",
            "filter.example.clean=",
            "-c",
            "filter.example.process=",
            "-c",
            "filter.example.required=false",
            "-c",
            "merge.example.driver=",
            "status",
            "--porcelain",
        ]
        return " output \n"

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree_git.git(tmp_path, "status", "--porcelain") == "output"


@pytest.mark.parametrize("socket", [None, "/example/signing.sock"])
def test_host_git_ignores_parent_git_and_other_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket: str | None
) -> None:
    monkeypatch.setenv("GIT_DIR", "/example/untrusted")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/example/untrusted")
    monkeypatch.setenv("EXAMPLE_PARENT", "private")
    monkeypatch.setenv("DEVELOPER_DIR", str(tmp_path / "untrusted-developer"))
    if socket is None:
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    else:
        monkeypatch.setenv("SSH_AUTH_SOCK", socket)
    test_git_suppresses_command_errors_and_returns_text(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "plant",
    [
        "reference-transaction",
        "post-checkout",
        "fsmonitor",
        "uploadpack",
        "ext",
        "smudge",
        "credential",
        "ssh",
    ],
)
def test_each_repo_command_plant_is_inert(tmp_path: Path, plant: str) -> None:
    _, clone = repository(tmp_path)
    marker = tmp_path / "executed"
    executable = tmp_path / "plant"
    executable.write_text(f'#!/bin/sh\n: > "{marker}"\nexit 1\n')
    executable.chmod(0o700)
    if plant in ("reference-transaction", "post-checkout"):
        hook = clone / ".git" / "hooks" / plant
        hook.write_text(executable.read_text())
        hook.chmod(0o700)
    elif plant == "fsmonitor":
        git(clone, "config", "core.fsmonitor", str(executable))
    elif plant == "uploadpack":
        git(clone, "config", "remote.origin.url", (tmp_path / "origin").as_uri())
        git(clone, "config", "remote.origin.uploadpack", str(executable))
    elif plant == "ext":
        git(clone, "config", "remote.origin.url", f"ext::{executable}")
    elif plant == "smudge":
        name = "planted"
        (clone / ".git" / "info" / "attributes").write_text("* filter=planted\n")
        for field in ("smudge", "clean", "process"):
            git(clone, "config", f"filter.{name}.{field}", str(executable))
        git(clone, "config", f"filter.{name}.required", "true")
        git(clone, "config", "merge.planted.driver", str(executable))
    elif plant == "credential":
        git(clone, "config", "credential.helper", f"!{executable}")
    else:
        git(clone, "config", "core.sshCommand", str(executable))
    worktree_checkout.create(clone, "safe", run_request())
    assert not marker.exists()


@pytest.mark.parametrize(
    ("verb", "flag"),
    [
        ("fetch", "http.followRedirects=false"),
        ("ls-remote", "http.followRedirects=false"),
        ("status", "protocol.allow=never"),
    ],
)
def test_online_commands_never_follow_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, flag: str
) -> None:
    seen: list[list[str]] = []

    def output(command: list[str], **_kwargs: object) -> str:
        seen.append(command)
        return ""

    monkeypatch.setattr(subprocess, "check_output", output)
    worktree_git.git(tmp_path, verb)
    assert flag in seen[-1]
    assert ("http.followRedirects=false" in seen[-1]) is (verb != "status")


def test_fetch_never_receives_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "sensitive-socket")
    monkeypatch.setenv("GH_TOKEN", "sensitive-token")
    calls: list[list[str]] = []

    def output(command: list[str], **kwargs: object) -> str:
        calls.append(command)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["GIT_ASKPASS"] == "/bin/false"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "SSH_AUTH_SOCK" not in environment
        assert "GH_TOKEN" not in environment
        assert "credential.helper=" in command
        assert "core.askPass=" in command
        assert not any("sensitive" in value for value in command)
        if "--get-regexp" in command:
            raise subprocess.CalledProcessError(1, command)
        assert command[-2:] == ["fetch", "origin"]
        return ""

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree_git.git(tmp_path, "fetch", "origin") == ""
    assert len(calls) == 2


def test_driver_overrides_are_complete() -> None:
    assert worktree_git.driver_overrides(
        "filter.example.smudge\0filter.example.required\0merge.example.driver\0merge.example.name\0other.key\0"
    ) == [
        "-c",
        "filter.example.smudge=",
        "-c",
        "filter.example.clean=",
        "-c",
        "filter.example.process=",
        "-c",
        "filter.example.required=false",
        "-c",
        "merge.example.driver=",
    ]


@pytest.mark.parametrize("code", [1, 2])
def test_config_enumeration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def output(command: list[str], **_kwargs: object) -> str:
        if "--get-regexp" in command:
            raise subprocess.CalledProcessError(code, command)
        return "complete"

    monkeypatch.setattr(subprocess, "check_output", output)
    if code == 1:
        assert worktree_git.git(tmp_path, "status") == "complete"
    else:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            worktree_git.git(tmp_path, "status")
        assert caught.value.returncode == 2


def test_filter_subsection_newlines_are_data() -> None:
    assert worktree_git.driver_overrides("filter.line\nbreak.smudge\0") == [
        "-c",
        "filter.line\nbreak.smudge=",
        "-c",
        "filter.line\nbreak.clean=",
        "-c",
        "filter.line\nbreak.process=",
        "-c",
        "filter.line\nbreak.required=false",
    ]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("merge.example.name", []),
        ("other.example.driver", []),
        ("merge.example.driver", ["-c", "merge.example.driver="]),
    ],
)
def test_only_merge_drivers_are_overridden(key: str, expected: list[str]) -> None:
    assert worktree_git.driver_overrides(key + "\0") == expected


@pytest.mark.parametrize("command", ["fetch", "ls-remote", "status"])
def test_host_git_disables_prune_lazy_fetch_and_offline_protocols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    calls: list[list[str]] = []

    def output(argv: list[str], **kwargs: object) -> str:
        calls.append(argv)
        assert "gc.auto=0" in argv
        assert "maintenance.auto=false" in argv
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["GIT_NO_LAZY_FETCH"] == "1"
        offline = "--get-regexp" in argv or command == "status"
        assert ("protocol.allow=never" in argv) is offline
        return ""

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree_git.git(tmp_path, command) == ""
    assert len(calls) == 2


def test_real_git_calls_have_bounded_operation_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, clone = repository(tmp_path)
    git(seed, "config", "filter.example.smudge", "cat")
    output = subprocess.check_output
    calls: list[tuple[list[str], int]] = []

    def checked(command: list[str], **kwargs: Unpack[HostGitOptions]) -> str:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int)
        calls.append((command, timeout))
        assert "transfer.fsckObjects=true" in command
        return output(command, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", checked)
    worktree_checkout.create(clone, "timeouts", run_request())
    worktree_git.git(seed, "fetch", "origin")
    for command, timeout in calls:
        assert timeout == (300 if "fetch" in command or "clone" in command else 30)


def test_every_git_call_has_a_fixed_host_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    output = subprocess.check_output
    calls: list[str] = []

    def checked(command: list[str], **kwargs: Unpack[HostGitOptions]) -> str:
        assert kwargs.get("cwd") == "/"
        calls.append("/")
        return output(command, **kwargs)

    monkeypatch.chdir(clone)
    monkeypatch.setattr(subprocess, "check_output", checked)
    worktree_checkout.create(clone, "cwd", run_request())
    assert calls


def test_git_clone_command_spells_metadata_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()

    def output(command: list[str], **_kwargs: object) -> str:
        index = command.index("--git-dir")
        assert command[index : index + 4] == [
            "--git-dir",
            str(tmp_path / ".git"),
            "--work-tree",
            str(tmp_path),
        ]
        return ""

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree_git.git(tmp_path, "status") == ""


@pytest.mark.parametrize("command", ["init", "fetch", "clone", "status"])
@pytest.mark.parametrize("drivers", [False, True])
def test_git_operation_timeouts_without_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, drivers: bool
) -> None:
    calls: list[list[str]] = []

    def output(argv: list[str], **kwargs: object) -> str:
        calls.append(argv)
        if "--get-regexp" in argv:
            assert kwargs["timeout"] == 30
            if drivers:
                return "filter.fixture.smudge\0"
            raise subprocess.CalledProcessError(1, argv)
        assert argv[-1] == command
        assert kwargs["timeout"] == (300 if command in ("fetch", "clone") else 30)
        return " output \n"

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree_git.git(tmp_path, command) == "output"
    assert len(calls) == (1 if command == "init" else 2)


def test_git_sends_the_token_as_an_environment_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, dict[str, str]]] = []

    def check_output(command: object, **kwargs: object) -> str:
        env = kwargs["env"]
        assert isinstance(env, dict)
        seen.append((command, env))
        return ""

    monkeypatch.setattr(subprocess, "check_output", check_output)
    value = "example-credential"
    worktree_git.git(tmp_path, "init", token=value)
    command, env = seen[0]
    basic = base64.b64encode(b"x-access-token:example-credential").decode()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {basic}"
    assert "example-credential" not in " ".join(map(str, command))  # type: ignore[call-overload]
    worktree_git.git(tmp_path, "init")
    assert "GIT_CONFIG_COUNT" not in seen[1][1]
