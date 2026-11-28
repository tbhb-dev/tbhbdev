"""Clean up ended runs' records, Bazel servers, and output folders on this host."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

from tbhbdev.alpha.agent import bazel_servers, process_group, runfiles
from tbhbdev.alpha.agent.decisions import run_state, runfiles_names
from tbhbdev.alpha.agent.model import NAME, Invalid
from tbhbdev.alpha.agent.run_host import bazel_cache

DISK_ALERT, END_RC, END_REASON = 90, 137, "process gone"


def _read_opt(path: Path, limit: int = 65536) -> str | None:
    try:
        return runfiles.read_owned(path, limit, path.name)
    except FileNotFoundError:
        return None


def _lock_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise Invalid(str(path))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _group_alive(path: Path) -> bool:
    if (raw := _read_opt(path)) is None:
        return False
    rec = run_state.Group(**json.loads(raw))
    return run_state.owns(rec, process_group.identity(rec.pgid))


class _Sweeper:
    def __init__(self, home: Path, *, dry_run: bool) -> None:
        self.home, self.dry_run = home, dry_run
        self.runs_dir = runfiles.state_home(home) / runfiles_names.DIRECTORY
        self.ended = self.servers = self.outputs = self.errors = 0
        self.live: list[Path | None] = []
        self.ended_runs: dict[str, Path | None] = {}
        self.srvs: dict[Path, tuple[bazel_servers.Server, Path]] = {}

    def _say(self, verb: str, detail: str) -> None:
        print(f"{'dry-run: ' if self.dry_run else ''}{verb} {detail}")

    def _err(self, context: str, detail: object) -> None:
        self.errors += 1
        print(f"error: {context}: {detail}", file=sys.stderr)

    def _mark_ended(self, name: str, entry: dict[str, object] | None) -> None:
        self.ended += 1
        self._say("mark ended", f"{name} ({END_REASON})")
        if self.dry_run:
            return
        f = runfiles.RunFiles(self.runs_dir, name)
        rc = int(raw.strip()) if (raw := _read_opt(f.path("rc"))) else END_RC
        f.write("log", f"sweep: marked ended ({END_REASON})\n")
        f.write("phase", "ended")
        f.write("rc", f"{rc}\n")
        if entry is not None:
            end = {"state": "ended", "ended": time.time(), "rc": rc}
            runfiles.append(self.runs_dir, {**entry, **end, "end_reason": END_REASON})

    def _inspect_one(self, name: str, entry: dict[str, object] | None) -> None:
        v = entry.get("worktree") if entry is not None else None
        wt = Path(v) if isinstance(v, str) and v else None
        try:
            d = self.runs_dir
            if _lock_held(d / f"{name}.run.lock") or _group_alive(d / f"{name}.pgid"):
                self.live.append(wt)
                return
            ph = (_read_opt(d / f"{name}.phase") or "").strip()
            idx = bool(entry and entry.get("state") == "running")
            mid = ph not in {"", "ended", "unknown"} and not _read_opt(d / f"{name}.rc")
            if idx or ph == "running" or mid:
                self._mark_ended(name, entry)
            self.ended_runs[name] = wt
        except (Invalid, OSError, ValueError, TypeError, KeyError) as exc:
            self._err(f"run {name}", exc)
            self.live.append(wt)

    def inspect_runs(self) -> None:
        if not self.runs_dir.exists():
            return
        st = self.runs_dir.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            self._err(str(self.runs_dir), "not an owned directory")
            return
        raw = _read_opt(self.runs_dir / runfiles.INDEX, 64 << 20) or ""
        index = {str(d["run"]): d for d in map(json.loads, raw.splitlines())}
        names = {n for n in index if NAME.fullmatch(n)}
        for e in self.runs_dir.iterdir():
            for s in (".phase", ".pgid", ".run.lock", ".rc"):
                if e.name.endswith(s) and NAME.fullmatch(k := e.name.removesuffix(s)):
                    names.add(k)
        for name in sorted(names):
            self._inspect_one(name, index.get(name))

    def _stop(self, base: Path, srv: bazel_servers.Server) -> bool:
        if self.dry_run or (out := bazel_servers.stop(base, srv)) == "rc=0":
            self._say("stop bazel server", f"{srv.pid} ({base})")
            self.servers += 1
            return True
        self._err(f"stop bazel server {srv.pid} ({base})", out)
        return False

    def _bases(self, wts: list[Path], skip: set[Path]) -> dict[Path, Path]:
        all_b = (b for w in wts for b in bazel_cache.bazel_bases(self.home, str(w)))
        return {r: b for b in all_b if (r := b.resolve()) not in skip}

    def _stop_ended(
        self, cands: dict[Path, Path], ew: list[Path], prot: set[Path]
    ) -> set[Path]:
        stopped: set[Path] = set()
        for b, (srv, p) in list(self.srvs.items()):
            if b not in prot and (b in cands or any(p.is_relative_to(w) for w in ew)):
                cands.setdefault(b, b)
                if self._stop(b, srv):
                    stopped.add(b)
                    self.srvs.pop(b, None)
        return stopped

    def sweep_bazel(self) -> None:
        sv = self.srvs = bazel_servers.running()
        lw = [w.resolve() for w in self.live if w]
        ew = [w.resolve() for w in self.ended_runs.values() if w]
        in_lw = {b for b, (_, p) in sv.items() if any(p.is_relative_to(w) for w in lw)}
        prot = set(self._bases(lw, set())) | in_lw
        cands = self._bases(ew, prot)
        stopped = self._stop_ended(cands, ew, prot)
        for b in bazel_cache.output_bases(self.home):
            if (r := b.resolve()) not in prot:
                cands.setdefault(r, b)
        self._remove_bases(cands, stopped)

    def _remove_bases(self, cands: dict[Path, Path], stopped: set[Path]) -> None:
        for res, base in sorted(cands.items()):
            if res in self.srvs:
                continue
            try:
                if self.dry_run:
                    live = res not in stopped and bazel_cache.server_alive(base)
                    ok = not live and bool(bazel_cache.owned_base(self.home, base))
                else:
                    ok = bazel_cache.remove_unused_base(self.home, base)
                if ok:
                    self._say("remove bazel output", str(base))
                    self.outputs += 1
            except OSError as exc:
                self._err(f"remove bazel output {base}", exc)


def sweep_checkouts(ended: dict[str, Path | None], *, dry_run: bool) -> None:
    """Extension point for part 2b: remove checkouts of ended runs."""
    del ended, dry_run


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    s = _Sweeper(home := Path.home(), dry_run=args.dry_run)
    s.inspect_runs()
    s.sweep_bazel()
    sweep_checkouts(s.ended_runs, dry_run=args.dry_run)
    u = shutil.disk_usage(home)
    disk = (u.used * 100) // max(u.total, 1)
    tail = f"outputs_removed={s.outputs} errors={s.errors} disk={disk}%"
    print(f"summary: ended={s.ended} servers_stopped={s.servers} {tail}")
    return 1 if disk >= DISK_ALERT else 0
