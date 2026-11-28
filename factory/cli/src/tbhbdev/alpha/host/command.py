"""`tbhbdev alpha host check`: one cheap, read-only verdict on this host.

Writes `~/.local/state/tbhbdev/host-fitness.json` and prints a table, or one JSON
line with `--json`. Exit status: 0 when fit, 1 when unfit, 2 when the check failed.
Informational checks (load, memory, admitted runs) never make a host unfit.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import subprocess  # nosec B404
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import import_module, metadata
from pathlib import Path

from tbhbdev import userconfig, version
from tbhbdev.alpha import command, tool_pins
from tbhbdev.alpha.agent import host_tools, settings
from tbhbdev.alpha.agent.decisions import limits, remote_host, repo
from tbhbdev.alpha.agent.model import DEFAULT_RUN_MEMORY_MIB, Host, Os

# Each limit is one constant; change it here.
MAX_COMMITS_BEHIND = 50
MAX_HOURS_BEHIND = 6
MIN_FREE_PERCENT = 15
MAX_INODE_PERCENT = 80
PINS_FILE = "mise.toml"
REFSPEC = "+refs/heads/main:refs/remotes/origin/main"


@dataclass(frozen=True)
class Check:
    """One verdict. REPAIR is a one-word hint, `none` when OK."""

    name: str
    ok: bool
    value: str
    limit: str
    repair: str = "none"


def _verdict(name: str, value: str, limit: str, repair: str, *, ok: bool) -> Check:
    return Check(name, ok, value, limit, "none" if ok else repair)


def _git(clone: Path, *args: str) -> str:
    done = tool_pins.tool_command(
        ["git", "-C", str(clone), *args],
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        timeout=10,
    )
    if done.returncode:
        raise OSError(done.stderr.strip() or f"git {args[0]} failed")
    return done.stdout


def revision() -> str:
    """The running build, or `unknown`."""
    try:
        return version.line().partition("(")[2].removesuffix(")") or version.UNKNOWN
    except metadata.PackageNotFoundError, OSError, ValueError:
        return version.UNKNOWN


def cli_check() -> Check:
    """The running CLI knows its revision and every alpha command module imports."""
    broken = []
    for module, _ in command.COMMAND_MODULES.values():
        try:
            import_module(module)
        except ImportError, SyntaxError, OSError, ValueError:
            broken.append(module)
    found = revision()
    value = f"cannot import {', '.join(broken)}" if broken else found
    ok = not broken and found != version.UNKNOWN
    return _verdict("cli", value, "known revision", "reinstall-cli", ok=ok)


def clone_check(clone: Path, *, fetch: bool) -> Check:
    """The clone has an origin whose main it is not too far behind, after a fetch."""
    limit = f"{MAX_COMMITS_BEHIND} commits, {MAX_HOURS_BEHIND} hours"
    if not clone.is_dir():
        return _verdict("clone", "missing", limit, "fetch-clone", ok=False)
    try:
        _git(clone, "remote", "get-url", "origin")
        if fetch:
            _git(clone, "fetch", "-q", "--no-tags", "origin", REFSPEC)
        behind = int(_git(clone, "rev-list", "--count", "main..origin/main"))
        times = _git(clone, "show", "-s", "--format=%ct", "main", "origin/main").split()
        hours = max(0, int(times[1]) - int(times[0])) / 3600 if behind else 0
    except (OSError, ValueError, IndexError) as exc:
        return _verdict("clone", str(exc), limit, "fetch-clone", ok=False)
    ok = behind <= MAX_COMMITS_BEHIND and hours <= MAX_HOURS_BEHIND
    value = f"{behind} commits, {hours:.1f} hours behind"
    return _verdict("clone", value, limit, "fetch-clone", ok=ok)


def tools_check(clone: Path, home: Path) -> Check:
    """Every check tool on the account's path is at the pin in origin/main."""
    limit = f"pins in origin/main:{PINS_FILE}"
    env = {"PATH": ":".join(host_tools.account_path(str(home))), "HOME": str(home)}
    try:
        text = _git(clone, "show", f"origin/main:{PINS_FILE}")
        with tempfile.TemporaryDirectory() as top:
            (Path(top) / PINS_FILE).write_text(text)
            pins, problems = tool_pins.check_tools(Path(top), env, home)
    except (OSError, ValueError, TypeError) as exc:
        value = f"cannot read pins: {exc}"
        return _verdict("tools", value, limit, "fetch-clone", ok=False)
    value = "; ".join(problems.values()) or f"{len(pins)} tools at their pins"
    return _verdict("tools", value, limit, "install-tools", ok=not problems)


def disk_check(home: Path) -> Check:
    """Free space on the home filesystem and the Bazel output root."""
    roots = {"home": home, "bazel": home / ".cache/bazel"}
    stats = {name: os.statvfs(path) for name, path in roots.items() if path.exists()}
    free = {name: 100 * stat.f_bavail / stat.f_blocks for name, stat in stats.items()}
    value = ", ".join(f"{name} {percent:.0f}% free" for name, percent in free.items())
    ok = all(percent >= MIN_FREE_PERCENT for percent in free.values())
    return _verdict("disk", value, f">= {MIN_FREE_PERCENT}% free", "clean-disk", ok=ok)


def inode_check() -> Check:
    """Inode use of the temporary directory, `/tmp` unless TMPDIR says otherwise."""
    stat = os.statvfs(tempfile.gettempdir())
    used = 100 * (stat.f_files - stat.f_ffree) / stat.f_files if stat.f_files else 0
    limit = f"< {MAX_INODE_PERCENT}% used"
    ok = used < MAX_INODE_PERCENT
    return _verdict("tmp-inodes", f"{used:.0f}% used", limit, "clean-tmp", ok=ok)


def _host() -> tuple[Host, int]:
    """This host's entry in the local settings, else the defaults, with the memory."""
    here = socket.gethostname().partition(".")[0]
    try:
        loaded = settings.load()
    except userconfig.ConfigError, OSError, ValueError:
        loaded = None
    memory = loaded.run_memory_mib if loaded else DEFAULT_RUN_MEMORY_MIB
    for host in loaded.hosts if loaded else ():
        if host.name == here:
            return host, memory
    user = getpass.getuser()
    system = Os.MACOS if sys.platform == "darwin" else Os.LINUX
    host = Host(here, system, "sh", user, (user,))
    return host, memory


def _probe(host: Host) -> str:
    """The launcher's probe, run here."""
    return tool_pins.tool_command(["sh", "-c", remote_host.limits(host)]).stdout


def load_checks() -> list[Check]:
    """Load, memory and admitted runs as the launcher reads them; information only."""
    host, memory = _host()
    found, admitted = limits.parse_probe(_probe(host))
    _, message = limits.decide(host, found, memory, admitted=admitted, ignore=False)
    return [
        Check("load", ok=True, value=message or "within limits", limit="launcher"),
        Check("admitted", ok=True, value=str(admitted), limit="none"),
    ]


def run_all(clone: Path, home: Path, *, fetch: bool) -> list[Check]:
    return [
        cli_check(),
        clone_check(clone, fetch=fetch),
        tools_check(clone, home),
        disk_check(home),
        inode_check(),
        *load_checks(),
    ]


def write(path: Path, data: dict[str, object]) -> None:
    """Replace PATH with DATA as JSON, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".host-fitness-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("verb", choices=["check"])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)
    home, start = Path.home(), time.monotonic()
    try:
        clone = Path(repo.clone_path(home, "tbhb-dev/tbhbdev"))
        found = run_all(clone, home, fetch=not args.no_fetch)
        report: dict[str, object] = {
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "revision": revision(),
            "fit": all(check.ok for check in found),
            "seconds": round(time.monotonic() - start, 2),
            "checks": [asdict(check) for check in found],
        }
        write(home / ".local/state/tbhbdev/host-fitness.json", report)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report))
    else:
        for check in found:
            print(f"{'ok  ' if check.ok else 'FAIL'} {check.name:<10} {check.value}")
        print(f"fit={report['fit']} {report['seconds']}s")
    return 0 if report["fit"] else 1
