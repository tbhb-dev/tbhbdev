"""Hook installation and validation for run clones."""

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
    worktree_checkout,
    worktree_hooks,
)
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.identity.commit import policy as identity_commit_policy
from tbhbdev.alpha.install import scripts as git_templates
from tbhbdev.alpha.repo import TBHB_DEV_ORIGIN


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
            run_env=identity_commit_policy.RUN_HOOK_ENV,
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


def test_hook_lock_covers_nested_directories_and_data(tmp_path: Path) -> None:
    (tmp_path / ".git/hooks/nested").mkdir(parents=True)
    data = tmp_path / ".git/hooks/nested/data"
    data.write_text("fixture")
    data.chmod(0o644)
    worktree_hooks.install_hooks(tmp_path, "example", "a" * 40)
    assert data.stat().st_mode & 0o777 == 0o444
    assert data.parent.stat().st_mode & 0o777 == 0o555
    assert data.parent.parent.stat().st_mode & 0o777 == 0o555


@pytest.mark.parametrize("mode", [0o001, 0o010, 0o100])
def test_hook_lock_preserves_executable_type(tmp_path: Path, mode: int) -> None:
    (tmp_path / ".git/hooks").mkdir(parents=True)
    executable = tmp_path / ".git/hooks/extra-script"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(mode)
    worktree_hooks.install_hooks(tmp_path, "example", "a" * 40)
    assert executable.stat().st_mode & 0o777 == 0o555
