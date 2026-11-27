"""Tests for create a standalone run clone and prepare its Git metadata."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from agent_host_example import (
    commit,
    git,
    repository,
    run_request,
)

from tbhbdev.alpha.agent import (
    worktree_checkout,
    worktree_clone,
    worktree_git,
    worktree_mirror,
)
from tbhbdev.alpha.agent.model import Invalid, Profile

pytestmark = pytest.mark.usefixtures("scratch_cwd")


def test_copying_clone_has_no_shared_object_inode(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    path = worktree_checkout.create(clone, "example", run_request())
    mirror = worktree_mirror.mirror_path(tmp_path, run_request().repo)
    mirror_inodes = {
        p.stat().st_ino for p in (mirror / "objects").rglob("*") if p.is_file()
    }
    run_inodes = {
        p.stat().st_ino for p in (path / ".git" / "objects").rglob("*") if p.is_file()
    }
    assert mirror_inodes
    assert run_inodes
    assert mirror_inodes.isdisjoint(run_inodes)
    assert not (path / ".git" / "objects" / "info" / "alternates").exists()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_clone_destination_is_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    workspace = tmp_path / "run"
    if kind == "directory":
        workspace.mkdir()
    elif kind == "file":
        workspace.touch()
    else:
        workspace.symlink_to(tmp_path / "missing", target_is_directory=True)

    def unexpected(*_args: object) -> str:
        pytest.fail("git must not touch an existing destination")

    monkeypatch.setattr(worktree_git, "git", unexpected)
    with pytest.raises(FileExistsError):
        worktree_clone.clone_run(
            tmp_path / "mirror", workspace, "example", Profile.BUILD
        )


def test_clone_directory_mode_is_private_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, int]] = []
    original = Path.mkdir

    def mkdir(path: Path, mode: int = 0o777) -> None:
        calls.append((path, mode))
        original(path, mode=mode)

    def git_command(*_args: object) -> str:
        assert calls == [(tmp_path / "workspace", 0o700)]
        return ""

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(worktree_git, "git", git_command)
    worktree_clone.clone_run(
        tmp_path / "mirror", tmp_path / "workspace", "example", Profile.BUILD
    )


def test_host_checkout_disables_a_planted_run_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    original = worktree_clone.clone_run
    marker = tmp_path / "hook-executed"

    def planted(mirror: Path, workspace: Path, url: str, profile: Profile) -> None:
        original(mirror, workspace, url, profile)
        hook = workspace / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir()
        hook.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\n')
        hook.chmod(0o700)

    monkeypatch.setattr(worktree_clone, "clone_run", planted)
    path = worktree_checkout.create(clone, "planted", run_request())
    assert (path / "tracked").read_text() == "initial\n"
    assert not marker.exists()


def test_git_shape_is_prepared_and_repeatable(tmp_path: Path) -> None:
    directory = tmp_path / ".git"
    directory.mkdir()
    worktree_clone.prepare_git(tmp_path)
    assert (directory / "config.worktree").read_text() == ""
    assert (directory / "commondir").read_text() == ".\n"
    assert list((directory / "modules").iterdir()) == []
    worktree_clone.prepare_git(tmp_path)


@pytest.mark.parametrize("name", ["config.worktree", "modules", "commondir"])
@pytest.mark.parametrize("kind", ["symlink", "broken-symlink", "content", "wrong-type"])
def test_git_shape_refuses_redirected_or_unexpected_entries(
    tmp_path: Path, name: str, kind: str
) -> None:
    directory = tmp_path / ".git"
    directory.mkdir()
    entry = directory / name
    if "symlink" in kind:
        target = tmp_path / "target"
        if kind == "symlink":
            target.write_text("untouched")
        entry.symlink_to(target)
    elif name == "modules" and kind == "content":
        entry.mkdir()
        (entry / "plant").touch()
    elif (name == "modules") == (kind == "wrong-type"):
        entry.write_text("unexpected")
    else:
        entry.mkdir()
    with pytest.raises(Invalid) as caught:
        worktree_clone.prepare_git(tmp_path)
    assert str(caught.value) == f"unexpected git metadata: {name}"


def test_git_shape_follows_checkout_and_hooks_and_supports_real_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    original_git = worktree_git.git
    original_shape = worktree_clone.prepare_git
    checked: list[str] = []

    def git_before_shape(path: Path, *args: str) -> str:
        if args[0] == "checkout":
            assert not (path / ".git/config.worktree").exists()
            assert not (path / ".git/modules").exists()
            assert not (path / ".git/commondir").exists()
            checked.append("checkout")
        return original_git(path, *args)

    def shape_after_hooks(path: Path) -> None:
        assert checked == ["checkout"]
        assert (path / "tracked").read_text() == "initial\n"
        assert (path / ".git/hooks/pre-commit").is_file()
        checked.append("shape")
        original_shape(path)

    monkeypatch.setattr(worktree_git, "git", git_before_shape)
    monkeypatch.setattr(worktree_clone, "prepare_git", shape_after_hooks)
    workspace = worktree_checkout.create(clone, "shape", run_request())
    assert checked == ["checkout", "shape"]
    before = git(workspace, "rev-parse", "HEAD")
    (workspace / "tracked").write_text("committed\n")
    assert commit(workspace, "shape fixture") != before
    assert git(workspace, "status", "--porcelain") == ""


def test_git_shape_writes_exact_paths_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / ".git"
    directory.mkdir()
    mkdir = Path.mkdir
    write_text = Path.write_text
    calls: list[tuple[str, str, str]] = []

    def make(path: Path) -> None:
        calls.append(("mkdir", str(path), ""))
        mkdir(path)

    def write(path: Path, text: str) -> int:
        calls.append(("write", str(path), text))
        return write_text(path, text)

    monkeypatch.setattr(Path, "mkdir", make)
    monkeypatch.setattr(Path, "write_text", write)
    worktree_clone.prepare_git(tmp_path)
    assert calls == [
        ("write", str(directory / "config.worktree"), ""),
        ("mkdir", str(directory / "info"), ""),
        ("mkdir", str(directory / "modules"), ""),
        ("write", str(directory / "commondir"), ".\n"),
    ]


def test_build_git_uses_the_run_gh_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    worktree_git.git(mirror, "init", "--bare", "--template=")
    clone = tmp_path / "clone"
    worktree_clone.clone_run(
        mirror, clone, "https://example.test/repo.git", Profile.BUILD
    )
    helper = tmp_path / "gh"
    helper.write_text('#!/bin/sh\ncat "$GH_CONFIG_DIR/credential"\n')
    helper.chmod(0o700)
    value = "username=example\npassword=example-run-credential\n"
    (tmp_path / "credential").write_text(value)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    config = tmp_path / "global"
    config.write_text('[credential]\nhelper = "!echo username=x; echo password=x"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    output = subprocess.check_output(
        ["git", "-C", str(clone), "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        timeout=10,
    )
    assert output.endswith(value)
