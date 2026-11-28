"""Clean up ended runs' records, Bazel servers, outputs, and checkouts."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import stat
import subprocess  # nosec B404
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import bazel_servers, process_group, push, runfiles
from tbhbdev.alpha.agent.decisions import run_state, runfiles_names
from tbhbdev.alpha.agent.model import NAME, Invalid
from tbhbdev.alpha.agent.run_host import bazel_cache
from tbhbdev.alpha.agent.worktree_git import GIT_OVERRIDES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DISK_ALERT, END_RC, END_REASON = 90, 137, "process gone"
MIN_AGE, _MIN_WORKTREE_PARTS = 2 * 3600, 4


@dataclass(frozen=True)
class EndedRun:
    worktree: Path | None
    ended: float | None = None


@dataclass(frozen=True)
class CheckoutCounts:
    removed: int = 0
    kept_unpushed: int = 0
    kept_other: int = 0


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
        self.ended_runs: dict[str, EndedRun] = {}
        self.srvs: dict[Path, tuple[bazel_servers.Server, Path]] = {}

    def _say(self, verb: str, detail: str) -> None:
        print(f"{'dry-run: ' if self.dry_run else ''}{verb} {detail}")

    def _err(self, context: str, detail: object) -> None:
        self.errors += 1
        print(f"error: {context}: {detail}", file=sys.stderr)

    def _mark_ended(self, name: str, entry: dict[str, object] | None) -> float:
        now = time.time()
        self.ended += 1
        self._say("mark ended", f"{name} ({END_REASON})")
        if self.dry_run:
            return now
        f = runfiles.RunFiles(self.runs_dir, name)
        rc = int(raw.strip()) if (raw := _read_opt(f.path("rc"))) else END_RC
        f.write("log", f"sweep: marked ended ({END_REASON})\n")
        f.write("phase", "ended")
        f.write("rc", f"{rc}\n")
        if entry is not None:
            end = {"state": "ended", "ended": now, "rc": rc}
            runfiles.append(self.runs_dir, {**entry, **end, "end_reason": END_REASON})
        return now

    def _inspect_one(self, name: str, entry: dict[str, object] | None) -> None:
        v = entry.get("worktree") if entry is not None else None
        wt = Path(v) if isinstance(v, str) and v else None
        raw_end = entry.get("ended") if entry is not None else None
        ended_at = (
            float(raw_end)
            if isinstance(raw_end, int | float) and not isinstance(raw_end, bool)
            else None
        )
        try:
            d = self.runs_dir
            if _lock_held(d / f"{name}.run.lock") or _group_alive(d / f"{name}.pgid"):
                self.live.append(wt)
                return
            ph = (_read_opt(d / f"{name}.phase") or "").strip()
            idx = bool(entry and entry.get("state") == "running")
            mid = ph not in {"", "ended", "unknown"} and not _read_opt(d / f"{name}.rc")
            if idx or ph == "running" or mid:
                ended_at = self._mark_ended(name, entry)
            self.ended_runs[name] = EndedRun(wt, ended_at)
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
        ew = [r.worktree.resolve() for r in self.ended_runs.values() if r.worktree]
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


_FETCH = ("fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules")
_GIT_OPTS = [x for v in GIT_OVERRIDES for x in ("-c", v)]
_GIT_ENV = dict.fromkeys(("GIT_NO_REPLACE_OBJECTS", "GIT_NO_LAZY_FETCH"), "1")
_GIT_ENV |= dict.fromkeys(("GIT_ASKPASS", "SSH_ASKPASS"), "/bin/false")


def _git(wt: Path, *args: str) -> str:
    net = args[0] in {"fetch", "ls-remote"}
    flag = "http.followRedirects=false" if net else "protocol.allow=never"
    out = push.history_git(wt, *_GIT_OPTS, "-c", flag, *args, extra_env=_GIT_ENV)
    return out.strip()


def _overlaps_any(wt: Path, live: Sequence[Path | None]) -> bool:
    res = wt.resolve()
    return any(
        a == b or a.is_relative_to(b) or b.is_relative_to(a)
        for raw in live
        if raw is not None
        for a, b in ((wt, raw), (res, raw.resolve()))
    )


def _checkout_anchor(home: Path, runs_dir: Path, wt: Path) -> Path | None:
    if not wt.is_absolute() or ".." in wt.parts:
        return None
    for b in dict.fromkeys((home, home.resolve())):
        if wt.is_relative_to(b):
            p = wt.relative_to(b).parts
            if len(p) >= _MIN_WORKTREE_PARTS and (p[0], p[-2]) == (
                "Code",
                ".worktrees",
            ):
                return b
    for b in dict.fromkeys((runs_dir, runs_dir.resolve())):
        if wt.is_relative_to(b) and wt != b:
            return b
    return None


def _eligible_anchor(
    home: Path, runs: Path, wt: Path, live: Sequence[Path | None], *, ok: bool
) -> Path | None:
    if not ok or _overlaps_any(wt, live):
        return None
    anchor = _checkout_anchor(home, runs, wt)
    if anchor is None or (opened := bazel_cache.open_owned_dir(anchor, wt)) is None:
        return None
    os.close(opened[0])
    return anchor


def _commit_on_remote(wt: Path, head: str, *, dry_run: bool) -> bool:
    remote = _git(wt, "remote", "get-url", "--push", "origin")
    if dry_run:
        out = _git(wt, "ls-remote", "--heads", "--end-of-options", remote)
        tips = sorted({ln.split("\t", 1)[0] for ln in out.splitlines() if "\t" in ln})
        rev = ("rev-list", "--ignore-missing", "-n", "1", f"--ancestry-path={head}")
        return head in tips or (
            bool(tips) and bool(_git(wt, *rev, f"^{head}", *tips, "--"))
        )
    refspec = "+refs/heads/*:refs/remotes/origin/*"
    _git(wt, *_FETCH, "--prune", "--end-of-options", remote, refspec)
    q = ("for-each-ref", "--contains", head, "--format=%(refname)")
    return bool(_git(wt, *q, "refs/remotes/origin"))


def _checkout_state(wt: Path, *, dry_run: bool) -> str:
    try:
        head = _git(wt, "rev-parse", "--verify", "HEAD^{commit}")
        st = ("status", "--porcelain", "--untracked-files=normal", "--ignored=no")
        if _git(wt, *st) or _git(wt, "stash", "list"):
            return "unpushed"
        return "remove" if _commit_on_remote(wt, head, dry_run=dry_run) else "unpushed"
    except subprocess.SubprocessError, OSError:
        return "other"


def _remove_run_records(runs_dir: Path, name: str) -> None:
    bazel_cache.remove_owned_dir(runs_dir, runs_dir / name)
    with suppress(OSError):
        fd = os.open(runs_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for suffix in runfiles_names.SUFFIXES:
                entry = f"{name}{suffix}"
                with suppress(OSError):
                    st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                        os.unlink(entry, dir_fd=fd)
        finally:
            os.close(fd)


def _remove_checkout(anchor: Path, wt: Path, runs_dir: Path, name: str) -> bool:
    common: Path | None = None
    if (dot := wt / ".git").is_file() and not dot.is_symlink():
        with suppress(subprocess.SubprocessError, OSError):
            q = ("rev-parse", "--path-format=absolute", "--git-common-dir")
            common = Path(_git(wt, *q))
    if not bazel_cache.remove_owned_dir(anchor, wt):
        return False
    if common is not None:
        with suppress(subprocess.SubprocessError, OSError):
            _git(common, "worktree", "prune")
    _remove_run_records(runs_dir, name)
    return True


def _lstat_ok(wt: Path) -> bool | None:
    try:
        wt.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return False
    return True


def sweep_checkouts(
    ended: Mapping[str, EndedRun],
    *,
    home: Path,
    runs_dir: Path,
    live: Sequence[Path | None] = (),
    dry_run: bool = False,
) -> CheckoutCounts:
    """Remove clean, pushed checkouts of ended runs older than two hours."""
    cutoff, prefix = time.time() - MIN_AGE, "dry-run: " if dry_run else ""
    removed = unpushed = other = 0
    for name, run in sorted(ended.items()):
        if (wt := run.worktree) is None or (ok := _lstat_ok(wt)) is None:
            continue
        anchor = _eligible_anchor(home, runs_dir, wt, live, ok=ok)
        if anchor is None or run.ended is None or run.ended >= cutoff:
            other += 1
            continue
        state = _checkout_state(wt, dry_run=dry_run)
        if state == "unpushed":
            unpushed += 1
            print(f"{prefix}keep checkout {name} (unpushed work)")
        elif state == "remove" and (
            dry_run or _remove_checkout(anchor, wt, runs_dir, name)
        ):
            removed += 1
            print(f"{prefix}remove checkout {name} ({wt})")
        else:
            other += 1
    return CheckoutCounts(removed, unpushed, other)


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
    c = sweep_checkouts(
        s.ended_runs, home=home, runs_dir=s.runs_dir, live=s.live, dry_run=args.dry_run
    )
    u = shutil.disk_usage(home)
    disk = (u.used * 100) // max(u.total, 1)
    ch = f"checkouts_removed={c.removed} checkouts_kept_unpushed={c.kept_unpushed}"
    tail = f"outputs_removed={s.outputs} {ch} checkouts_kept_other={c.kept_other}"
    print(
        f"summary: ended={s.ended} servers_stopped={s.servers} {tail}"
        f" errors={s.errors} disk={disk}%"
    )
    return 1 if disk >= DISK_ALERT else 0
