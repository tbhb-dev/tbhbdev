"""Exercise local run-end sweep rules, dry-run, isolation, and summary exit codes."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_cli_example import run_agent

from tbhbdev.alpha.agent import bazel_servers, process_group, runfiles
from tbhbdev.alpha.agent.decisions import runfiles_names
from tbhbdev.alpha.agent.run_host import bazel_cache

if TYPE_CHECKING:
    from pathlib import Path


def _age(base: Path) -> None:
    past = time.time() - 3600
    os.utime(base, (past, past))


def _srv(b: Path, ws: Path, pid: int) -> str:
    (b / "server").mkdir(parents=True, exist_ok=True)
    (b / "server/server.pid.txt").write_text(f"{pid}\n")
    flags = f"--output_base={b}\0--workspace_directory={ws}"
    (b / "server/cmdline").write_text(f"bazel(w)\0{flags}")
    _age(b)
    return f"{os.getuid()} {pid} 1 1 bazel(w) {flags.replace(chr(0), ' ')} -f"


class _Env:
    def __init__(
        self, h: Path, r: Path, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str]
    ) -> None:
        self.h, self.r, self.mp, self.cap = h, r, mp, cap

    def mk(self, name: str, st: str = "running") -> tuple[Path, Path]:
        wt = (self.h / f"wt-{name}").resolve()
        wt.mkdir(exist_ok=True)
        b = bazel_cache.bazel_bases(self.h, str(wt))[0]
        b.mkdir(parents=True, exist_ok=True)
        _age(b)
        row = json.dumps({"run": name, "worktree": str(wt), "state": st})
        with (self.r / runfiles.INDEX).open("a") as out:
            out.write(row + "\n")
        (self.r / f"{name}.phase").write_text(st)
        return wt, b

    def srvs(self, *rows: str) -> None:
        self.mp.setattr(bazel_servers, "listing", Mock(return_value="\n".join(rows)))

    def run(self, *args: str) -> tuple[int, str, str]:
        rc = run_agent(["sweep", *args])
        out, err = self.cap.readouterr()
        return rc, out, err

    def check(
        self, out: str, c: tuple[int, int, int, int], disk: int = 40, ko: int = 0
    ) -> None:
        ck = f"checkouts_removed=0 checkouts_kept_unpushed=0 checkouts_kept_other={ko}"
        tail = f"outputs_removed={c[2]} {ck} errors={c[3]} disk={disk}%"
        want = f"summary: ended={c[0]} servers_stopped={c[1]} {tail}"
        assert [ln for ln in out.splitlines() if ln.startswith("summary:")] == [want]


@pytest.fixture
def env(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> _Env:
    disk = SimpleNamespace(total=100, used=40, free=60)
    monkeypatch.setattr(bazel_cache, "VAR_TMP", isolated_home / "vt")
    monkeypatch.setattr(bazel_servers, "MEMORY_DISK", isolated_home / "shm")
    monkeypatch.setattr(bazel_servers, "listing", Mock(return_value=""))
    monkeypatch.setattr(shutil, "disk_usage", Mock(return_value=disk))
    runs = runfiles.state_home(isolated_home) / runfiles_names.DIRECTORY
    return _Env(isolated_home, runfiles.private_dir(runs), monkeypatch, capsys)


def test_live_kept_dead_marked_ended_and_orphans_cleaned(env: _Env) -> None:
    r = env.r
    _, b_lk = env.mk("live-lock")
    wt_pg, b_pg = env.mk("live-pgid")
    _, b_dead = env.mk("dead-run")
    (r / "dead-run.run.lock").write_text("")
    (r / "dead-run.pgid").write_text('{"pgid": 999999999, "boot": "old", "start": "1"}')
    (r / "phase-only.phase").write_text("workspace")

    def fake_start(pid: int) -> str | None:
        return "s" if pid in {os.getpid(), 6001} else None

    env.mp.setattr(process_group, "leader_start", fake_start)
    process_group.record(runfiles.RunFiles(r, "live-pgid"), os.getpid())
    custom = env.h / "shm" / "live-custom"
    env.srvs(_srv(custom, wt_pg, 6001))
    p = b_dead.parent
    orphan, inst, ext = p / ("a" * 32), p / "install", p / ("b" * 32)
    for d in (orphan, inst, ext / "server"):
        d.mkdir(parents=True)
    _age(orphan)
    _age(ext)
    (ext / "server/server.pid.txt").write_text(str(os.getpid()))
    with (r / "live-lock.run.lock").open("w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, out, _ = env.run()
    assert rc == 0
    env.check(out, (2, 0, 2, 0), ko=1)
    assert (b_lk.is_dir(), b_pg.is_dir(), custom.is_dir()) == (True, True, True)
    assert (r / "live-lock.phase").read_text() == "running"
    assert not (r / "live-pgid.rc").exists()
    assert (r / "dead-run.phase").read_text() == "ended"
    assert (r / "dead-run.rc").read_text() == "137\n"
    assert "process gone" in (r / "dead-run.log").read_text()
    assert runfiles.latest(r)["dead-run"]["end_reason"] == "process gone"
    assert (r / "phase-only.phase").read_text() == "ended"
    assert (b_dead.exists(), orphan.exists()) == (False, False)
    assert (inst.is_dir(), ext.is_dir()) == (True, True)


@pytest.mark.parametrize("args", [(), ("--dry-run",)])
def test_recent_base_without_pid_survives(env: _Env, args: tuple[str, ...]) -> None:
    _, ended = env.mk("recent-ended", "ended")
    recent, old = ended.parent / ("a" * 32), ended.parent / ("b" * 32)
    recent.mkdir()
    old.mkdir()
    _age(old)
    ended.touch()

    rc, out, _ = env.run(*args)

    assert rc == 0
    env.check(out, (0, 0, 1, 0), ko=1)
    assert str(old) in out
    assert recent.is_dir()
    assert ended.is_dir()
    assert old.exists() == bool(args)


def test_old_base_of_run_started_after_snapshot_survives(env: _Env) -> None:
    wt = (env.h / "wt-new-owner").resolve()
    base = bazel_cache.bazel_bases(env.h, str(wt))[0]
    base.mkdir(parents=True)
    _age(base)

    def register(*_args: object) -> str:
        env.mk("new-owner")
        process_group.record(runfiles.RunFiles(env.r, "new-owner"), os.getpid())
        return ""

    env.mp.setattr(bazel_servers, "listing", register)
    rc, out, _ = env.run()

    assert rc == 0
    env.check(out, (0, 0, 0, 0))
    assert base.is_dir()


def test_ended_run_stops_bazel_by_process_and_removes_output(env: _Env) -> None:
    wt, base = env.mk("ended-run", "ended")
    env.mp.setattr(subprocess, "run", client := Mock())
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"]) as p:
        try:
            env.srvs(_srv(base, wt, p.pid))
            rc, out, _ = env.run()
            assert p.wait(timeout=2) == -signal.SIGKILL
        finally:
            p.kill()
    client.assert_not_called()
    assert (rc, base.exists()) == (0, False)
    env.check(out, (0, 1, 1, 0), ko=1)


def test_hung_server_stop_does_not_stop_sweep(env: _Env) -> None:
    wt_h, b_h = env.mk("hung-run", "ended")
    wt_ok, b_ok = env.mk("ok-run", "ended")
    env.srvs(_srv(b_h, wt_h, 999_999_998), _srv(b_ok, wt_ok, 999_999_999))
    env.mp.setattr(process_group, "leader_start", Mock(return_value="s"))
    env.mp.setattr(bazel_servers, "stop", Mock(side_effect=["server alive", "rc=0"]))
    rc, out, err = env.run()
    assert (rc, "server alive" in err) == (0, True)
    assert (b_h.is_dir(), b_ok.exists()) == (True, False)
    env.check(out, (0, 1, 1, 1), ko=2)


def test_dry_run_changes_nothing(env: _Env) -> None:
    wt, base = env.mk("dry")
    env.srvs(_srv(base, wt, 5001))
    env.mp.setattr(process_group, "leader_start", Mock(return_value="s"))
    env.mp.setattr(bazel_servers, "stop", stop := Mock(return_value="rc=0"))
    before = sorted(p.name for p in env.r.iterdir())
    rc, out, _ = env.run("--dry-run")
    assert (rc, sorted(p.name for p in env.r.iterdir())) == (0, before)
    for want in ("mark ended dry", "stop bazel server 5001", "remove bazel output"):
        assert f"dry-run: {want}" in out
    env.check(out, (1, 1, 1, 0), ko=1)
    assert ((env.r / "dry.phase").read_text(), base.is_dir()) == ("running", True)
    stop.assert_not_called()


@pytest.mark.parametrize(("used", "want_rc"), [(89, 0), (90, 1)])
def test_disk_threshold_and_help(env: _Env, used: int, want_rc: int) -> None:
    env.r.rmdir()
    disk = SimpleNamespace(total=100, used=used, free=100 - used)
    env.mp.setattr(shutil, "disk_usage", Mock(return_value=disk))
    rc, out, _ = env.run()
    assert rc == want_rc
    env.check(out, (0, 0, 0, 0), disk=used)
    assert "--dry-run" in env.run("--help")[1]


def test_failing_run_and_foreign_owner_isolated(env: _Env) -> None:
    r = env.r
    _, b_bad = env.mk("bad-run")
    _, b_ok = env.mk("good-run")
    (r / "bad-run.pgid").write_text("{corrupt")
    (r / "sym-lock.run.lock").mkdir()
    bad_b = b_ok.parent / ("0" * 32)
    bad_b.mkdir()
    _age(bad_b)
    real_rm = bazel_cache.remove_unused_base

    def flaky(home: Path, b: Path) -> bool:
        if b == bad_b:
            raise OSError(str(b))
        return real_rm(home, b)

    env.mp.setattr(bazel_cache, "remove_unused_base", flaky)
    rc, out, err = env.run()
    assert (rc, "bad-run" in err, str(bad_b) in err) == (0, True, True)
    env.check(out, (1, 0, 1, 3), ko=1)
    assert (b_bad.is_dir(), b_ok.exists()) == (True, False)
    assert (r / "good-run.phase").read_text() == "ended"
    env.mp.setattr(os, "getuid", Mock(return_value=os.getuid() + 1))
    assert "not an owned directory" in env.run()[2]
