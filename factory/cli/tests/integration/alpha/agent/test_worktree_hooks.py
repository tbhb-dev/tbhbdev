"""Tests for install and verify the Git hooks for a run clone."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Unpack

import pytest
from agent_host_example import (
    HostGitOptions,
    commit,
    git,
    repository,
    run_request,
)

from tbhbdev.alpha.agent import (
    host_tools,
    worktree_checkout,
    worktree_hooks,
)
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.identity import dispatcher as identity
from tbhbdev.alpha.install import scripts as git_templates
from tbhbdev.alpha.repo import TBHB_DEV_ORIGIN

pytestmark = pytest.mark.usefixtures("scratch_cwd")


def test_hook_installation_is_repeatable_private_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    original = Path.mkdir
    calls: list[tuple[str, int, bool]] = []

    def mkdir(path: Path, mode: int = 0o777, *, exist_ok: bool = False) -> None:
        calls.append((str(path), mode, exist_ok))
        original(path, mode=mode, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    worktree_hooks.install_hooks(tmp_path, "example", "a" * 40)
    worktree_hooks.install_hooks(tmp_path, "example", "a" * 40)
    assert calls == [(str(tmp_path / ".git" / "hooks"), 0o700, True)] * 2
    hooks = tmp_path / ".git" / "hooks"
    assert hooks.stat().st_mode & 0o777 == 0o555
    assert {p.name for p in hooks.iterdir()} == {
        "pre-commit",
        "pre-push",
        "pre-push.prek",
        "reference-transaction",
        "prepare-commit-msg",
        "prepare-commit-msg.prek",
        "commit-msg",
        "commit-msg.prek",
        "post-commit",
        "post-commit.prek",
    }
    assert all(p.stat().st_mode & 0o777 == 0o555 for p in hooks.iterdir())
    for name, verb in git_templates.IDENTITY_HOOKS:
        assert (hooks / name).read_text() == worktree_hooks.RUN_IDENTITY_SHIM.format(
            mark=git_templates.SHIM_MARK,
            verb=verb,
            hook=name,
            run_env=identity.RUN_HOOK_ENV,
        )
    for name in (
        "pre-commit",
        "pre-push",
        *(p[0] for p in git_templates.IDENTITY_HOOKS),
    ):
        destination = name if name == "pre-commit" else f"{name}.prek"
        assert (hooks / destination).read_text() == worktree_hooks.RUN_PREK_SHIM.format(
            prek="/example/prek", stage=name
        )
    assert (hooks / "pre-push").read_text() == git_templates.pre_push_shim(
        Path(sys.base_prefix, "bin", "python3"),
        TBHB_DEV_ORIGIN.pattern,
        {
            "TBHBDEV_RUN_NAME": "example",
            "TBHBDEV_RUN_START_REF": "",
            "TBHBDEV_RUN_START_TIP": "a" * 40,
        },
    )


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("disabled", r"^effective core\.hooksPath does not select the run's hooks$"),
        ("missing", "run hook reference-transaction is missing"),
        ("mode", "run hook reference-transaction is missing or not executable"),
    ],
)
def test_run_hook_verification_refuses_invalid_git_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, message: str
) -> None:
    _, clone = repository(tmp_path)
    workspace = worktree_checkout.create(clone, "example", run_request())
    hook = workspace / ".git/hooks/reference-transaction"
    env = dict(os.environ)
    if failure == "disabled":
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="core.hooksPath",
            GIT_CONFIG_VALUE_0=os.devnull,
        )
    elif failure == "missing":
        hook.parent.chmod(0o755)
        hook.unlink()
    else:
        hook.chmod(0o444)
    output = subprocess.check_output

    def probe(command: list[str], **options: Unpack[HostGitOptions]) -> str:
        assert options == {
            "cwd": workspace,
            "env": env,
            "stderr": subprocess.PIPE,
            "text": True,
            "timeout": 30,
        }
        return output(command, **options)

    monkeypatch.setattr(subprocess, "check_output", probe)
    if failure == "disabled":
        with pytest.raises(Invalid, match=message):
            worktree_hooks.verify_run_hooks(workspace, env)
    else:
        with pytest.raises(Invalid, match=message):
            worktree_hooks.verify_hook_files(workspace)


def test_hook_installation_precedes_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    install = worktree_hooks.install_hooks

    def checked(path: Path, name: str, start: str, *, start_ref: str | None) -> None:
        assert not (path / "tracked").exists()
        assert name == "ordered"
        assert start == git(clone, "rev-parse", "HEAD")
        install(path, name, start, start_ref=start_ref)

    monkeypatch.setattr(worktree_hooks, "install_hooks", checked)
    worktree_checkout.create(clone, "ordered", run_request())


def test_run_reference_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, clone = repository(tmp_path)
    outside = git(seed, "rev-parse", "HEAD")
    start = commit(seed, "run start")
    git(seed, "push", "origin", "main")
    path = worktree_checkout.create(clone, "example", run_request())
    branch = "refs/heads/agent/example"

    binary = tmp_path / "bin"
    binary.mkdir()
    tool = binary / "tbhbdev"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}:{os.environ['PATH']}")
    git(path, "config", "user.name", "Example")
    git(path, "config", "user.email", "example@example.test")
    git(path, "config", "commit.gpgsign", "false")

    for args, reason in (
        (("checkout", "-b", "other"), f"refs/heads/other may only update {branch}"),
        (("reset", "--hard", outside), f"{branch} must descend from {start}"),
    ):
        result = subprocess.run(
            ["git", *args], cwd=path, capture_output=True, text=True, check=False
        )
        assert result.returncode != 0
        assert f"reference-transaction: {reason}" in result.stderr
        assert git(path, "rev-parse", "HEAD") == start

    (path / "run.txt").write_text("run change\n")
    git(path, "add", "run.txt")
    git(path, "commit", "-qm", "run change")
    upstream = commit(seed, "upstream change")
    git(seed, "push", "origin", "main")
    git(path, "fetch", str(tmp_path / "origin"), "main:refs/remotes/upstream/main")
    git(path, "rebase", "upstream/main")
    assert git(path, "rev-parse", "HEAD^") == upstream

    (path / "run.txt").write_text("stashed change\n")
    git(path, "stash", "push")
    git(path, "stash", "pop")
    assert (path / "run.txt").read_text() == "stashed change\n"
    git(path, "restore", "run.txt")
    git(path, "tag", "point")
    git(path, "switch", "--detach", outside)
    git(path, "commit", "--allow-empty", "-qm", "detached change")
    git(path, "branch", "-D", "agent/example")
    assert git(path, "for-each-ref", branch) == ""
    assert git(path, "rev-parse", "point^") == upstream


@pytest.mark.parametrize("scope", ["local", "global", "system", "include", "command"])
@pytest.mark.parametrize("writable", [False, True])
def test_hooks_path_refuses_other_locations_in_every_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, writable: bool
) -> None:
    _, workspace = repository(tmp_path)
    target = tmp_path / "other-hooks"
    if scope == "local":
        git(workspace, "config", "core.hooksPath", str(target))
    elif scope == "command":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(target))
    else:
        config = tmp_path / "hooks-config"
        config.write_text(f"[core]\n hooksPath = {target}\n")
        if scope == "include":
            git(workspace, "config", "include.path", str(config))
        else:
            monkeypatch.setenv(f"GIT_CONFIG_{scope.upper()}", str(config))
            monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    with pytest.raises(Invalid, match=r"core\.hooksPath") as caught:
        worktree_hooks.validate_hooks(
            workspace, (str(target.parent),) if writable else ()
        )
    reason = f" under write root {target.parent}" if writable else ""
    assert (
        str(caught.value)
        == f"core.hooksPath {target}{reason} is not the workspace hooks directory"
    )


@pytest.mark.parametrize("value", ["/dev/null", ".git/hooks", "own", "unset"])
def test_hooks_path_allows_own_hooks_and_dev_null(tmp_path: Path, value: str) -> None:
    _, workspace = repository(tmp_path)
    if value != "unset":
        git(
            workspace,
            "config",
            "core.hooksPath",
            str(workspace / ".git/hooks") if value == "own" else value,
        )
    worktree_hooks.validate_hooks(workspace, (str(workspace),))


def test_hook_lock_covers_nested_directories_and_data(tmp_path: Path) -> None:
    (tmp_path / ".git/hooks/nested").mkdir(parents=True)
    data = tmp_path / ".git/hooks/nested/data"
    data.write_text("fixture")
    data.chmod(0o644)
    worktree_hooks.install_hooks(tmp_path, "example", "a" * 40)
    assert data.stat().st_mode & 0o777 == 0o444
    assert data.parent.stat().st_mode & 0o777 == 0o555
    assert data.parent.parent.stat().st_mode & 0o777 == 0o555


def test_hooks_query_uses_host_environment_and_disables_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    calls: list[list[str]] = []

    def output(command: list[str], **kwargs: object) -> str:
        assert command[:3] == [
            host_tools.resolve("git"),
            "-c",
            "core.hooksPath=/dev/null",
        ]
        assert command[3:5] == ["config", "--path"]
        assert command[5:] == ["--null", "--get-all", "core.hooksPath"]
        assert kwargs == {
            "cwd": tmp_path,
            "text": True,
            "stderr": subprocess.DEVNULL,
            "timeout": 30,
        }
        calls.append(command)
        return ".git/hooks\0/dev/null\0"

    monkeypatch.setattr(subprocess, "check_output", output)
    worktree_hooks.validate_hooks(tmp_path, ())
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["", "../hooks", "alias", "~/other-hooks"])
def test_hooks_path_rejects_relative_and_symlinked_locations(
    tmp_path: Path, value: str
) -> None:
    git(tmp_path, "init", "--template=")
    (tmp_path / "alias").symlink_to(tmp_path / "other-hooks")
    git(tmp_path, "config", "core.hooksPath", value)
    with pytest.raises(Invalid, match="under write root"):
        worktree_hooks.validate_hooks(
            tmp_path, (str(tmp_path / "neighbor"), str(tmp_path.parent))
        )


@pytest.mark.parametrize("safe_first", [False, True])
def test_hooks_path_checks_unsafe_values_even_when_overridden(
    tmp_path: Path, *, safe_first: bool
) -> None:
    git(tmp_path, "init", "--template=")
    unsafe = str(tmp_path / "bad-hooks")
    values = ("/dev/null", unsafe) if safe_first else (unsafe, "/dev/null")
    for value in values:
        git(tmp_path, "config", "--add", "core.hooksPath", value)
    with pytest.raises(Invalid, match="bad-hooks"):
        worktree_hooks.validate_hooks(tmp_path, (str(tmp_path / "neighbor"),))


def test_hooks_query_skips_an_uncreated_workspace(tmp_path: Path) -> None:
    worktree_hooks.validate_hooks(tmp_path, ())


@pytest.mark.parametrize("mode", [0o001, 0o010, 0o100])
def test_hook_lock_preserves_executable_type(tmp_path: Path, mode: int) -> None:
    (tmp_path / ".git/hooks").mkdir(parents=True)
    executable = tmp_path / ".git/hooks/extra-script"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(mode)
    worktree_hooks.install_hooks(tmp_path, "example", "a" * 40)
    assert executable.stat().st_mode & 0o777 == 0o555


def test_hooks_query_spells_git_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def directory(path: Path) -> bool:
        assert path == tmp_path / ".git"
        return True

    def output(*_args: object, **_kwargs: object) -> str:
        return "/dev/null\0"

    monkeypatch.setattr(Path, "is_dir", directory)
    monkeypatch.setattr(subprocess, "check_output", output)
    worktree_hooks.validate_hooks(tmp_path, ())
