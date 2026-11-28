"""Exercise run-end checkout sweep rules with real temporary Git repositories."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_cli_example import run_agent
from agent_host_example import commit, git, repository

from tbhbdev.alpha.agent import bazel_servers, runfiles
from tbhbdev.alpha.agent.decisions import runfiles_names
from tbhbdev.alpha.agent.run_host import bazel_cache

if TYPE_CHECKING:
    from pathlib import Path

OLD, RECENT = time.time() - 3 * 3600, time.time() - 1800
KEYS = ("checkouts_removed", "checkouts_kept_unpushed", "checkouts_kept_other")


def _counts(out: str) -> list[str]:
    line = next(ln for ln in out.splitlines() if ln.startswith("summary:"))
    fields = dict(part.split("=", 1) for part in line.split()[1:])
    return [fields[k] for k in KEYS]


class _Env:
    def __init__(
        self, h: Path, r: Path, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str]
    ) -> None:
        self.h, self.r, self.mp, self.cap = h, r, mp, cap
        self.origin, (self.seed, _) = h / "origin", repository(h)
        (self.seed / ".gitignore").write_text("*.ignored\n")
        commit(self.seed, "ignore")
        git(self.seed, "push", "origin", "main")
        self.clone = h / "Code/github.com/tbhb-dev/example-repo"
        self.clone.parent.mkdir(parents=True)
        git(self.clone.parent, "clone", str(self.origin), str(self.clone))
        (self.clone / ".worktrees").mkdir()

    def rec(self, name: str, wt: Path, st: str = "ended", t: float = OLD) -> None:
        end = {"ended": t} if st == "ended" else {}
        row = {"run": name, "worktree": str(wt), "state": st} | end
        with (self.r / runfiles.INDEX).open("a") as out:
            out.write(json.dumps(row) + "\n")
        (self.r / f"{name}.phase").write_text(st)
        (self.r / f"{name}.owner").write_text("host\n")

    def mk(
        self, name: str, t: float = OLD, st: str = "ended", wt: Path | None = None
    ) -> Path:
        dst = wt or (self.clone / ".worktrees" / f"agent-{name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        git(dst.parent, "clone", str(self.origin), str(dst))
        self.rec(name, dst, st, t)
        return dst

    def run(self, *args: str) -> tuple[int, str, str]:
        rc = run_agent(["sweep", *args])
        out, err = self.cap.readouterr()
        return rc, out, err


@pytest.fixture
def env(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> _Env:
    monkeypatch.setattr(bazel_cache, "VAR_TMP", isolated_home / "vt")
    monkeypatch.setattr(bazel_servers, "MEMORY_DISK", isolated_home / "shm")
    monkeypatch.setattr(bazel_servers, "listing", Mock(return_value=""))
    disk = SimpleNamespace(total=100, used=40, free=60)
    monkeypatch.setattr(shutil, "disk_usage", Mock(return_value=disk))
    runs = runfiles.state_home(isolated_home) / runfiles_names.DIRECTORY
    return _Env(isolated_home, runfiles.private_dir(runs), monkeypatch, capsys)


def test_unpushed_commits_kept_ignoring_stale_local_ref(env: _Env) -> None:
    wt = env.mk("unpushed-run")
    (wt / "tracked").write_text("local only\n")
    git(wt, "update-ref", "refs/remotes/origin/stale", commit(wt, "local only"))
    env.rec("gone-run", env.clone / ".worktrees/agent-gone-run")
    rc, out, _ = env.run()
    assert (rc, wt.is_dir(), (env.r / "unpushed-run.phase").is_file()) == (
        0,
        True,
        True,
    )
    assert ("unpushed-run" in out, _counts(out)) == (True, ["0", "1", "0"])


def test_dirty_and_untracked_kept_but_ignored_removed(env: _Env) -> None:
    mod, unt, ign = env.mk("mod-run"), env.mk("unt-run"), env.mk("ign-run")
    (mod / "tracked").write_text("edited\n")
    (unt / "notes.txt").write_text("new\n")
    (ign / "scratch.ignored").write_text("ok\n")
    rc, out, _ = env.run()
    assert (rc, mod.is_dir(), unt.is_dir(), ign.exists()) == (0, True, True, False)
    assert ("mod-run" in out, "unt-run" in out, _counts(out)) == (
        True, True, ["1", "2", "0"],
    )  # fmt: skip


def test_stash_keeps_checkout(env: _Env) -> None:
    wt = env.mk("stashed-run")
    (wt / "tracked").write_text("wip\n")
    git(wt, "-c", "user.name=E", "-c", "user.email=e@e", "stash", "push", "-m", "wip")
    rc, out, _ = env.run()
    assert (rc, wt.is_dir(), "stashed-run" in out) == (0, True, True)
    assert _counts(out) == ["0", "1", "0"]


def test_age_threshold_fetch_containment_and_record_order(env: _Env) -> None:
    old, recent = env.mk("old-run"), env.mk("recent-run", RECENT)
    in_runs, fail_rm = env.mk("runs-wt", wt=env.r / "runs-wt"), env.mk("fail-rm")
    git(old, "config", "remote.origin.url", str(env.h / "unused-mirror.git"))
    git(old, "config", "remote.origin.pushurl", str(env.origin))
    (env.seed / "tracked").write_text("upstream\n")
    commit(env.seed, "upstream")
    git(env.seed, "push", "origin", "main")
    (hooks := old / ".git/hooks").mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\n")
    hooks.chmod(0o555)
    (env.r / "old-run").mkdir()
    (env.r / "old-run.log").symlink_to(env.h / "keep-me")
    real_rm = bazel_cache.remove_owned_dir

    def flaky_rm(anchor: Path, target: Path) -> bool:
        return target != fail_rm and real_rm(anchor, target)

    env.mp.setattr(bazel_cache, "remove_owned_dir", flaky_rm)
    rc, out, _ = env.run()
    gone = (old.exists(), (env.r / "old-run").exists(), in_runs.exists())
    recs = ((env.r / "old-run.phase").exists(), (env.r / "old-run.owner").exists())
    kept = (recent.is_dir(), fail_rm.is_dir(), (env.r / "fail-rm.phase").is_file())
    assert (rc, gone, recs) == (0, (False, False, False), (False, False))
    assert ((env.r / "old-run.log").is_symlink(), kept) == (True, (True, True, True))
    assert _counts(out) == ["2", "0", "2"]


def test_unreachable_remote_and_unreadable_repo_kept_other(env: _Env) -> None:
    offline = env.mk("offline-run")
    git(offline, "remote", "set-url", "origin", str(env.h / "missing.git"))
    broken = env.clone / ".worktrees/agent-broken-run"
    broken.mkdir()
    (broken / ".git").write_text("not-a-gitdir\n")
    env.rec("broken-run", broken)
    rc, out, _ = env.run()
    assert (rc, offline.is_dir(), broken.is_dir(), _counts(out)) == (
        0, True, True, ["0", "0", "2"],
    )  # fmt: skip


def test_live_foreign_symlink_and_outside_paths_untouched(env: _Env) -> None:
    live, foreign = env.mk("live-run", st="running"), env.mk("foreign-run")
    nested = env.mk("nested-run", wt=live / "sub")
    (target := env.h / "symlink-target").mkdir()
    sym = env.clone / ".worktrees/agent-sym-run"
    sym.symlink_to(target, target_is_directory=True)
    env.rec("sym-run", sym)
    outside = env.mk("outside-run", wt=env.h.parent / "outside-wt")
    env.rec("dotdot-run", env.clone / ".worktrees/../.worktrees/agent-foreign-run")
    (blocker := env.clone / ".worktrees/agent-blocker").write_text("file\n")
    env.rec("notdir-run", blocker / "child")
    real_stat = os.stat

    def fake_stat(p: object, *a: object, **kw: object) -> object:
        res = real_stat(p, *a, **kw)  # type: ignore[arg-type]
        if p == foreign.name and "dir_fd" in kw:
            return SimpleNamespace(st_mode=res.st_mode, st_uid=os.getuid() + 1)
        return res

    env.mp.setattr(os, "stat", fake_stat)
    with (env.r / "live-run.run.lock").open("w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, out, _ = env.run()
    kept = (live, nested, foreign, sym, target, outside)
    assert (rc, all(p.exists() for p in kept), _counts(out)) == (
        0, True, ["0", "0", "6"],
    )  # fmt: skip


def test_dry_run_lists_changes_nothing_and_matches_real_counts(env: _Env) -> None:
    clean, unpushed = env.mk("clean-run"), env.mk("unpushed-run")
    offline = env.mk("offline-run")
    (unpushed / "tracked").write_text("ahead\n")
    commit(unpushed, "ahead")
    git(offline, "remote", "set-url", "origin", str(env.h / "nope.git"))
    git(env.seed, "push", "origin", "HEAD:refs/heads/extra-branch")
    rc_d, out_d, _ = env.run("--dry-run")
    assert (rc_d, "dry-run: remove checkout clean-run" in out_d) == (0, True)
    assert ("unpushed-run" in out_d, clean.is_dir()) == (True, True)
    assert (env.r / "clean-run.phase").is_file()
    no_ref = clean / ".git/refs/remotes/origin/extra-branch"
    assert (no_ref.exists(), (clean / ".git/FETCH_HEAD").exists()) == (False, False)
    rc_r, out_r, _ = env.run()
    got = (rc_r, clean.exists(), unpushed.is_dir(), offline.is_dir())
    assert (got, _counts(out_d), _counts(out_r)) == (
        (0, False, True, True), ["1", "1", "1"], ["1", "1", "1"],
    )  # fmt: skip


def test_local_only_branch_survives_worktree_removal(env: _Env) -> None:
    wt = env.clone / ".worktrees/agent-wt-run"
    git(env.clone, "worktree", "add", "-b", "local-only", str(wt), "origin/main")
    head = git(wt, "rev-parse", "HEAD")
    env.rec("wt-run", wt)
    rc, out, _ = env.run()
    assert (rc, wt.exists(), (env.r / "wt-run.phase").exists()) == (0, False, False)
    assert _counts(out) == ["1", "0", "0"]
    assert git(env.clone, "rev-parse", "--verify", "refs/heads/local-only") == head
    git(env.clone, "checkout", "local-only")
    assert git(env.clone, "branch", "--show-current") == "local-only"
