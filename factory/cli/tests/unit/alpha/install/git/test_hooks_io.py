"""Tests for install git hooks."""

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from unit.alpha.install.conftest import World, git, make_repo

from tbhbdev.alpha.install import scripts as git_templates
from tbhbdev.alpha.install.git import hooks as git_hooks
from tbhbdev.alpha.install.git import lock

if TYPE_CHECKING:
    from collections.abc import Generator


def test_install_git_hooks(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert git_hooks.install_git_hooks(None, dry_run=False) == 0
    out = capsys.readouterr().out
    assert out.endswith(
        f"tbhbdev alpha install: installed git hooks in {world.hooks}\n"
    )
    assert sorted(p.name for p in world.hooks.iterdir() if "." not in p.name) >= [
        "commit-msg",
        "post-commit",
        "pre-commit",
        "pre-push",
        "prepare-commit-msg",
    ]
    for name, verb in git_templates.IDENTITY_HOOKS:
        assert f"tbhbdev alpha identity {verb} " in (world.hooks / name).read_text()
    assert world.prek_calls()[0] == [
        "install",
        "--hook-type",
        "pre-commit",
        "--hook-type",
        "pre-push",
    ]
    assert not (world.hooks / lock.LOCK).exists()
    assert "pre-push.prek" in {path.name for path in world.hooks.iterdir()}
    assert stat.S_IMODE((world.hooks / "pre-push").stat().st_mode) == 0o755


def test_git_hooks_problem_outside_a_repository(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(world.home)
    assert git_hooks.git_hooks_problem() == "cannot locate Git's hooks directory"


def test_git_hooks_problem_rejects_an_executable_foreign_shim(world: World) -> None:
    hook = world.hooks / "pre-push"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    assert git_hooks.git_hooks_problem() == f"{hook} is missing the tbhbdev hook"


def test_git_hooks_problem_reports_a_read_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = world.hooks / "pre-push"
    hook.write_text(git_templates.SHIM_MARK)
    hook.chmod(0o755)
    error = PermissionError(errno.EACCES, os.strerror(errno.EACCES))
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", Mock(side_effect=error))
        assert git_hooks.git_hooks_problem() == f"cannot read {hook}: {error.strerror}"


def test_install_git_hooks_from_a_worktree_uses_the_shared_hooks(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(world.repo, "commit", "-q", "--allow-empty", "-m", "base")
    tree = tmp_path / "tree"
    git(world.repo, "worktree", "add", "-q", "-b", "tree", str(tree))
    assert git_hooks.install_git_hooks(tree, dry_run=False) == 0
    assert capsys.readouterr().out.endswith(f"git hooks in {world.hooks}\n")
    assert (world.hooks / "commit-msg").is_file()
    assert not (world.repo / ".git" / "worktrees" / "tree" / "hooks").exists()


def test_install_git_hooks_runs_git_and_prek_in_the_named_work_tree(
    world: World, tmp_path: Path
) -> None:
    """The process can be anywhere; everything runs in the clone `cwd` names."""
    world.stages(*(name for name, _ in git_templates.IDENTITY_HOOKS))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chdir(elsewhere)
    assert git_hooks.install_git_hooks(world.repo, dry_run=False) == 0
    assert world.prek_cwds() == {str(world.repo)}
    for name, _ in git_templates.IDENTITY_HOOKS:
        assert (world.hooks / f"{name}.prek").is_file()


def test_install_git_hooks_reads_the_named_clones_hooks_path(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(world.repo, "config", "core.hooksPath", "elsewhere")
    os.chdir(tmp_path)
    assert git_hooks.install_git_hooks(world.repo, dry_run=False) == 0
    assert "core.hooksPath is elsewhere" in capsys.readouterr().err
    assert world.prek_calls() == []


def test_install_git_hooks_in_a_path_with_a_space(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spaced = make_repo(tmp_path / "a b" / "repo")
    os.chdir(spaced)
    assert git_hooks.install_git_hooks(spaced, dry_run=False) == 0
    hooks = spaced / ".git" / "hooks"
    assert capsys.readouterr().out.endswith(f"installed git hooks in {hooks}\n")
    assert world.prek_cwds() == {str(spaced)}
    assert (hooks / "commit-msg").is_file()


def test_install_git_hooks_gives_up_on_a_held_lock(
    world: World,
    capsys: pytest.CaptureFixture[str],
    live_pid: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_dir = world.hooks / lock.LOCK
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text(f"{live_pid}\n")
    monkeypatch.setattr(git_hooks, "LOCK_WAIT", 0)
    assert git_hooks.install_git_hooks(None, dry_run=False) == 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha install: error: could not take {lock_dir} within 0s; "
        "if no other install is running, remove it\n"
    )
    assert world.prek_calls() == []


def test_install_git_hooks_dry_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert git_hooks.install_git_hooks(None, dry_run=True) == 0
    assert capsys.readouterr().out == (
        "would install prek's pre-commit and pre-push hooks and the "
        "prepare-commit-msg, commit-msg, post-commit identity hooks in "
        f"{world.hooks}\n"
    )
    assert world.prek_calls() == []
    assert sorted(p.name for p in world.hooks.iterdir() if p.suffix != ".sample") == []


def test_install_git_hooks_outside_a_work_tree(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_hooks.install_git_hooks(plain, dry_run=False) == 0
    assert capsys.readouterr().out == (
        "tbhbdev alpha install: not in a git work tree; skipping the git hooks\n"
    )
    assert world.prek_calls() == []


def test_install_git_hooks_outside_tbhb_dev(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = make_repo(tmp_path / "other", "git@github.com:someone/x.git")
    assert git_hooks.install_git_hooks(other, dry_run=False) == 0
    assert capsys.readouterr().out == (
        "tbhbdev alpha install: origin is not a tbhb-dev repository; "
        "skipping the git hooks\n"
    )
    assert world.prek_calls() == []


def test_install_git_hooks_leaves_a_hooks_path_alone(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    git(world.repo, "config", "core.hooksPath", "elsewhere")
    assert git_hooks.install_git_hooks(None, dry_run=False) == 0
    assert capsys.readouterr().err == (
        "tbhbdev alpha install: warning: core.hooksPath is elsewhere; "
        "not installing git hooks\n"
    )
    assert world.prek_calls() == []


def test_install_git_hooks_reports_a_failure(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.bin / "prek.fail").write_text("")
    assert git_hooks.install_git_hooks(None, dry_run=False) == 1
    assert capsys.readouterr().err.endswith(
        "tbhbdev alpha install: error: "
        "prek install --hook-type pre-commit --hook-type pre-push failed\n"
    )
    assert not (world.hooks / lock.LOCK).exists()


def test_install_git_hooks_reports_a_directory_at_a_hook(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = world.hooks / "post-commit"
    hook.mkdir(parents=True)
    assert git_hooks.install_git_hooks(None, dry_run=False) == 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha install: error: {hook} is not a file; "
        "remove it and run the install again\n"
    )
    assert not (world.hooks / lock.LOCK).exists()


def test_push_hook_rerun_drops_previous_shim_without_backup(world: World) -> None:
    assert git_hooks.install_git_hooks(None, dry_run=False) == 0
    first = (world.hooks / "pre-push").read_text()
    assert git_hooks.install_git_hooks(None, dry_run=False) == 0
    assert (world.hooks / "pre-push").read_text() == first
    assert not (world.hooks / "pre-push.legacy").exists()
    assert list(world.hooks.glob("pre-push.legacy.tbhb-backup.*")) == []


def test_foreign_push_hook_checked_before_dependencies(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = world.hooks / "pre-push"
    hook.write_text("foreign\n")

    def unavailable() -> list[str]:
        pytest.fail("foreign hook must be checked before looking for prek")

    monkeypatch.setattr(git_hooks, "find_prek", unavailable)
    assert git_hooks.install_git_hooks(None, dry_run=False) == 1
    assert str(hook) in capsys.readouterr().err


def test_foreign_push_hook_appearing_during_lock(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextmanager
    def acquired(hooks: Path, timeout: int) -> Generator[None]:
        assert timeout == 15
        (hooks / "pre-push").write_text("foreign\n")
        yield

    monkeypatch.setattr(git_hooks, "hooks_lock", acquired)
    assert git_hooks.install_git_hooks(None, dry_run=False) == 1
    assert "move it aside" in capsys.readouterr().err
    assert world.prek_calls() == []
    assert (world.hooks / "pre-push").read_text() == "foreign\n"
