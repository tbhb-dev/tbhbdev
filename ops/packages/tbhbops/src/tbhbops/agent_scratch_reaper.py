"""Reclaim idle agent scratch space; standalone for system Python 3 on hosts."""

from __future__ import annotations  # noqa: TID251 - Copied to system Python 3.9 hosts.

# cspell:ignore statvfs bavail frsize favail inodes
import argparse
import getpass
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import time
from contextlib import suppress
from pathlib import Path


def process_paths(uid: int, proc: Path = Path("/proc")) -> list[Path]:
    if sys.platform == "darwin":
        result = subprocess.run(  # nosec B603, B607
            ["lsof", "-u", str(uid), "-F", "n"],
            check=True,
            capture_output=True,
            text=True,
        )
        return [
            Path(line[1:])
            for line in result.stdout.splitlines()
            if line.startswith("n/")
        ]
    paths = []
    for process in proc.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != uid:
                continue
            links = [process / "cwd", *(process / "fd").iterdir()]
            for link in links:
                # Process exit or a closed descriptor during enumeration.
                with suppress(FileNotFoundError):
                    paths.append(link.readlink())
        except FileNotFoundError:
            continue
    return paths


def idle(path: Path, cutoff: float, device: int) -> bool:
    pending = [path]
    while pending:
        current = pending.pop()
        info = current.lstat()
        if info.st_dev != device or info.st_mtime > cutoff:
            return False
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(current) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    return True


def live_server(path: Path) -> bool:
    try:
        pid = int((path / "server/server.pid.txt").read_text())
        if pid <= 0:
            return False
        # Permission denial also proves the PID exists.
        with suppress(PermissionError):
            os.kill(pid, 0)
        if sys.platform == "darwin":
            command = subprocess.run(  # nosec B603, B607
                ["ps", "-o", "command=", "-p", str(pid)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        else:
            command = Path(f"/proc/{pid}/cmdline").read_text()
    except (FileNotFoundError, ProcessLookupError, ValueError):  # fmt: skip
        return False
    return "bazel" in command


def space(path: Path) -> None:
    info = getattr(os, "statvfs")(path)  # noqa: B009 - POSIX API on a POSIX host.
    print(
        f"agent-scratch-reaper: SPACE {path} "
        f"free_bytes={info.f_bavail * info.f_frsize} "
        f"total_bytes={info.f_blocks * info.f_frsize} "
        f"free_inodes={info.f_favail} total_inodes={info.f_files}"
    )


def candidate(
    path: Path, info: os.stat_result, active: list[Path], *, bazel: bool
) -> bool:
    if bazel:
        return (
            bool(re.fullmatch(r"[0-9a-fA-F]{32}", path.name))
            and stat.S_ISDIR(info.st_mode)
            and not live_server(path)
        )
    return (
        info.st_uid == getattr(os, "getuid")()  # noqa: B009 - POSIX API on a POSIX host.
        and not path.name.startswith((".", "tmux-", "ssh-", "systemd-"))
        and not any(p == path or path in p.parents for p in active)
    )


def remove(path: Path, info: os.stat_result) -> None:
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def reap(  # noqa: PLR0913 - Shared job loop.
    root: Path,
    *,
    bazel: bool,
    active: list[Path],
    cutoff: float,
    deadline: float,
    live: bool,
) -> int:
    failed = 0
    device = root.stat().st_dev
    for path in root.iterdir():
        if time.monotonic() >= deadline:
            print("agent-scratch-reaper: max-seconds reached; stopping")
            break
        try:
            info = path.lstat()
            age = (time.time() - info.st_mtime) / 3600
            action = "SKIP-IN-USE"
            if candidate(path, info, active, bazel=bazel) and idle(
                path, cutoff, device
            ):
                action = "DRYRUN"
                if live:
                    if time.monotonic() >= deadline:
                        print("agent-scratch-reaper: max-seconds reached; stopping")
                        break
                    remove(path, info)
                    action = "REMOVE"
            print(f"agent-scratch-reaper: {action} {path} {age:.1f}")
        except (OSError, subprocess.SubprocessError) as error:  # fmt: skip
            print(f"agent-scratch-reaper: ERROR {path}: {error}", file=sys.stderr)
            failed = 1
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--tmp-root", type=Path, default=Path("/tmp"))  # noqa: S108  # nosec B108
    parser.add_argument("--bazel-base", type=Path, nargs="+")
    parser.add_argument("--min-age-hours", type=float, default=24)
    parser.add_argument("--max-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    home = Path.home()
    defaults = [
        home / parent / f"_bazel_{getpass.getuser()}"
        for parent in (".cache/bazel", "Library/Caches/bazel")
    ]
    bases = (
        args.bazel_base
        if args.bazel_base is not None
        else [p for p in defaults if p.exists()]
    )
    deadline = time.monotonic() + args.max_seconds
    cutoff = time.time() - args.min_age_hours * 3600
    failed = 0
    for path in (args.tmp_root, home):
        space(path)
    try:
        active = process_paths(getattr(os, "getuid")())  # noqa: B009 - POSIX API on a POSIX host.
    except (OSError, subprocess.SubprocessError) as error:  # fmt: skip
        print(f"agent-scratch-reaper: skipping tmp job: {error}", file=sys.stderr)
        failed = 1
    else:
        failed |= reap(
            args.tmp_root.resolve(),
            bazel=False,
            active=active,
            cutoff=cutoff,
            deadline=deadline,
            live=args.live,
        )
    for base in bases:
        failed |= reap(
            base,
            bazel=True,
            active=[],
            cutoff=cutoff,
            deadline=deadline,
            live=args.live,
        )
    for path in (args.tmp_root, home):
        space(path)
    return failed


if __name__ == "__main__":
    sys.exit(main())
