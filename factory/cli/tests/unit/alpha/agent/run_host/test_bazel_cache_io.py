"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import os
import shutil
import time
from pathlib import Path

import pytest
from run_host_fixtures import (
    HOST_FIXTURES,
    bazel_base,
    bazel_run,
    fake_bazel,
    make_base,
    stop_and_clean,
)

from tbhbdev.alpha.agent.run_host import bazel_cache as host_bazel_cache
from tbhbdev.alpha.agent.run_host import bazel_shutdown as host_bazel_shutdown

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


def test_cleanup_deletes_only_the_recorded_base_and_its_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    worktree = Path(prepared.paths.worktree)
    base = make_base(prepared)
    other = base.parent / ("0" * 32)
    (base / "execroot/ws/locked").mkdir(parents=True)
    (base / "execroot/ws/locked/file").write_text("12345")
    (base / "execroot/ws/locked").chmod(0o500)
    (base / "execroot/ws/dead").mkdir()
    (base / "execroot/ws/dead").chmod(0)
    other.mkdir()
    (base / "execroot/ws/out").symlink_to(other)
    (worktree / "bazel-out").symlink_to(base / "execroot/ws")
    (worktree / ".bazel-ws").symlink_to(base)
    (worktree / "bazel-other").symlink_to(other)
    (worktree / "bazel-file").write_text("keep")
    fake_bazel(monkeypatch)
    spaces = iter([(100, 10), (4100, 14)])

    def space(_fd: int) -> tuple[int, int]:
        return next(spaces)

    monkeypatch.setattr(host_bazel_cache, "_space", space)
    freed = stop_and_clean(run, prepared)
    assert freed == {"bytes": 4000, "inodes": 4, "bases": 1}
    assert not base.exists()
    assert other.is_dir()
    assert sorted(path.name for path in worktree.iterdir()) == [
        "bazel-file",
        "bazel-other",
    ]
    assert "bazel cleanup: freed" in run.files.path("log").read_text()


def test_space_reads_statvfs(tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        free_bytes, free_inodes = host_bazel_cache._space(fd)  # noqa: SLF001
    finally:
        os.close(fd)
    assert free_bytes > 0
    assert free_inodes >= 0


def test_cleanup_ignores_a_swapped_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    worktree = Path(prepared.paths.worktree)
    base = make_base(prepared)
    victim = run.home / "victim"
    victim.mkdir()
    victim_base = bazel_base(run, victim)
    victim_base.mkdir(parents=True)
    (victim / "bazel-out").symlink_to(victim_base)
    shutil.rmtree(worktree)
    worktree.symlink_to(victim)
    fake_bazel(monkeypatch)
    assert stop_and_clean(run, prepared)["bases"] == 1
    assert not base.exists()
    assert victim_base.is_dir()
    assert (victim / "bazel-out").is_symlink()


def test_cleanup_refuses_a_symlinked_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = prepared.bases[0]
    victim = tmp_path / "victim"
    (victim / "_bazel_x").mkdir(parents=True)
    victim_base = victim / base.parent.name / base.name
    victim_base.mkdir(parents=True)
    (run.home / ".cache").symlink_to(victim)
    (victim / "bazel").mkdir()
    (victim / "bazel" / base.parent.name).symlink_to(victim / base.parent.name)
    calls: list[list[str]] = []
    fake_bazel(monkeypatch, calls=calls)
    assert stop_and_clean(run, prepared)["bases"] == 0
    assert victim_base.is_dir()
    assert calls == []
    assert "refused" in run.files.path("log").read_text()


def test_cleanup_refuses_a_symlinked_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "file").write_text("keep")
    prepared.bases[0].parent.mkdir(parents=True)
    prepared.bases[0].symlink_to(victim)
    fake_bazel(monkeypatch)
    assert stop_and_clean(run, prepared)["bases"] == 0
    assert (victim / "file").read_text() == "keep"
    assert host_bazel_cache.cleanup_bazel(run, None, {})["bases"] == 0


def test_cleanup_refuses_a_foreign_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    fake_bazel(monkeypatch)
    monkeypatch.setattr(os, "getuid", lambda: tmp_path.stat().st_uid + 1)
    assert stop_and_clean(run, prepared)["bases"] == 0
    assert base.is_dir()


def test_cleanup_refuses_paths_outside_the_cache_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "file").write_text("keep")
    prepared = dataclasses.replace(prepared, bases=(outside,))
    fake_bazel(monkeypatch)
    assert stop_and_clean(run, prepared)["bases"] == 0
    assert (outside / "file").read_text() == "keep"
    assert host_bazel_cache.owned_base(run.home, outside) is None
    file_base = prepared.bases[0]
    base = host_bazel_cache.bazel_bases(run.home, str(tmp_path))[0]
    base.parent.mkdir(parents=True)
    base.write_text("not a directory")
    assert host_bazel_cache.owned_base(run.home, base) is None
    assert file_base.exists()


def test_cleanup_keeps_a_base_whose_server_is_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    (base / "server").mkdir()
    (base / "server/server.pid.txt").write_text(str(os.getpid()))
    calls: list[list[str]] = []
    pauses: list[float] = []
    fake_bazel(monkeypatch, calls=calls)
    monkeypatch.setattr(time, "sleep", pauses.append)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    assert stopped[base][0] == "server alive"
    assert (len(calls), pauses) == (4, [2, 2, 2])
    assert host_bazel_cache.cleanup_bazel(run, prepared, stopped)["bases"] == 0
    assert base.is_dir()
    assert "kept" in run.files.path("log").read_text()


@pytest.mark.parametrize("text", ["garbage", "-1", "999999999", "999999999999"])
def test_a_dead_or_unreadable_server_pid_is_not_alive(
    tmp_path: Path, text: str
) -> None:
    (tmp_path / "server").mkdir()
    (tmp_path / "server/server.pid.txt").write_text(text)
    assert not host_bazel_cache.server_alive(tmp_path)


def test_cleanup_keeps_a_base_when_shutdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    fake_bazel(monkeypatch, code=1)
    assert stop_and_clean(run, prepared)["bases"] == 0
    assert base.is_dir()


def test_cleanup_refuses_a_base_swapped_after_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    fake_bazel(monkeypatch)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    base.rename(base.with_name("moved"))
    base.mkdir()
    (base / "file").write_text("replacement")
    assert host_bazel_cache.cleanup_bazel(run, prepared, stopped)["bases"] == 0
    assert (base / "file").is_file()


def test_server_alive_keeps_a_base_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "server").mkdir()
    (tmp_path / "server/server.pid.txt").write_text("1")

    def deny(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", deny)
    assert host_bazel_cache.server_alive(tmp_path)


def test_cleanup_keeps_a_base_whose_server_restarted_after_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    fake_bazel(monkeypatch)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    assert stopped[base][0] == "rc=0"
    (base / "server").mkdir()
    (base / "server/server.pid.txt").write_text(str(os.getpid()))
    assert host_bazel_cache.cleanup_bazel(run, prepared, stopped)["bases"] == 0
    assert base.is_dir()
    assert "(server alive)" in run.files.path("log").read_text()


def test_cleanup_refuses_a_base_swapped_for_a_symlink_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "file").write_text("keep")
    fake_bazel(monkeypatch)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    original = host_bazel_cache.server_alive

    def swap(path: Path) -> bool:
        base.rename(base.with_name("moved"))
        base.symlink_to(victim)
        return original(path)

    monkeypatch.setattr(host_bazel_cache, "server_alive", swap)
    assert host_bazel_cache.cleanup_bazel(run, prepared, stopped)["bases"] == 0
    assert (victim / "file").read_text() == "keep"
    assert "bazel cleanup failed" in run.files.path("log").read_text()


def test_cleanup_deletes_through_the_validated_ancestors_after_a_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    (base / "mine").write_text("x")
    root = base.parent
    victim = tmp_path / "victim"
    (victim / base.name).mkdir(parents=True)
    (victim / base.name / "file").write_text("keep")
    fake_bazel(monkeypatch)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    original = host_bazel_cache.server_alive

    def swap(path: Path) -> bool:
        root.rename(root.with_name("moved"))
        root.symlink_to(victim)
        return original(path)

    monkeypatch.setattr(host_bazel_cache, "server_alive", swap)
    assert host_bazel_cache.cleanup_bazel(run, prepared, stopped)["bases"] == 1
    assert (victim / base.name / "file").read_text() == "keep"
    assert not (root.with_name("moved") / base.name).exists()


def test_cleanup_refuses_a_base_replaced_by_another_directory_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    fake_bazel(monkeypatch)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    original = host_bazel_cache.server_alive

    def swap(path: Path) -> bool:
        base.rename(base.with_name("moved"))
        base.mkdir()
        (base / "file").write_text("replacement")
        return original(path)

    monkeypatch.setattr(host_bazel_cache, "server_alive", swap)
    assert host_bazel_cache.cleanup_bazel(run, prepared, stopped)["bases"] == 0
    assert (base / "file").is_file()


def test_cleanup_unlinks_relative_links_into_a_deleted_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    worktree = Path(prepared.paths.worktree)
    base = make_base(prepared)
    (base / "execroot").mkdir()
    (worktree / "bazel-rel").symlink_to(os.path.relpath(base / "execroot", worktree))
    (worktree / "bazel-keep").symlink_to("../nowhere")
    fake_bazel(monkeypatch)
    assert stop_and_clean(run, prepared)["bases"] == 1
    assert not (worktree / "bazel-rel").is_symlink()
    assert (worktree / "bazel-keep").is_symlink()


def test_cleanup_keeps_a_base_that_vanished_after_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    fake_bazel(monkeypatch)
    stopped = host_bazel_shutdown.shutdown_bazel(run, prepared)
    base.rmdir()
    assert host_bazel_cache.cleanup_bazel(run, prepared, stopped)["bases"] == 0
    assert "kept" in run.files.path("log").read_text()


def test_owned_base_is_none_when_the_anchor_is_missing(tmp_path: Path) -> None:
    home = tmp_path / "absent"
    base = host_bazel_cache.bazel_bases(home, str(tmp_path))[0]
    assert host_bazel_cache.owned_base(home, base) is None


def test_owned_base_rejects_a_cache_root_and_dot_dot(tmp_path: Path) -> None:
    run, prepared = bazel_run(tmp_path)
    base = make_base(prepared)
    root = base.parent
    assert host_bazel_cache.owned_base(run.home, root) is None
    assert host_bazel_cache.owned_base(run.home, root / "..") is None
    assert host_bazel_cache.owned_base(run.home, base) is not None


@pytest.mark.parametrize("mode", ["error", "left"])
def test_failed_deletion_reports_no_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    run, prepared = bazel_run(tmp_path)
    make_base(prepared)
    fake_bazel(monkeypatch)

    def rmtree(*_args: object, **_options: object) -> None:
        if mode == "error":
            raise OSError

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    assert stop_and_clean(run, prepared) == {"bytes": 0, "inodes": 0, "bases": 0}
    assert "bazel cleanup failed" in run.files.path("log").read_text()
