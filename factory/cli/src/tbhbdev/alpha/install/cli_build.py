"""Build and verify a uv tool environment before switching the live entry point."""

import shlex

# This program runs even when the host's installed CLI cannot be imported.
_INSTALL = r'''
import fcntl
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
import traceback
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

SHA = r"[0-9a-f]{40}"
PROOF = """
from importlib import import_module
from tbhbdev.alpha.command import COMMAND_MODULES

for module, function in COMMAND_MODULES.values():
    getattr(import_module(module), function)
"""


def requirement(row):
    name = row["name"]
    if row.get("extras"):
        name += "[" + ",".join(row["extras"]) + "]"
    if "editable" in row:
        return ["--with-editable", str(Path(row["editable"]).resolve())]
    url = row.get("url")
    fragments = {}
    if "git" in row:
        git = urlsplit(row["git"])
        options = dict(parse_qsl(git.query))
        ref = git.fragment or next(
            (options[key] for key in ("rev", "branch", "tag") if key in options), ""
        )
        path = git.path + ("@" + quote(ref, safe="/") if ref else "")
        url = "git+" + urlunsplit(git._replace(path=path, query="", fragment=""))
        fragments = {key: options[key] for key in ("subdirectory", "lfs", "path")
                     if key in options}
    for key in ("path", "directory"):
        if key in row:
            url = Path(row[key]).absolute().as_uri()
    if "subdirectory" in row:
        fragments["subdirectory"] = row["subdirectory"]
    if url and fragments:
        url += ("&" if "#" in url else "#") + urlencode(fragments)
    pin = f" @ {url}" if url else row.get("specifier", "")
    marker = f" ; {row['marker']}" if row.get("marker") else ""
    return ["--with", name + pin + marker]


def extras(entry):
    receipt = entry.resolve().parent.parent / "uv-receipt.toml"
    if not receipt.is_file():
        return []
    rows = tomllib.loads(receipt.read_text())["tool"]["requirements"]
    return [arg for row in rows if row["name"] != "tbhbdev" for arg in requirement(row)]


def replace_link(link, target):
    staged = link.with_name(f".{link.name}.{secrets.token_hex(8)}")
    staged.symlink_to(target)
    try:
        os.replace(staged, link)
    finally:
        staged.unlink(missing_ok=True)


def output(entry, *args):
    return subprocess.check_output([entry, *args], text=True).strip()


def stamp(entry):
    found = re.fullmatch(r"tbhbdev \S+ \((" + SHA + r")\)", output(entry, "--version"))
    return found[1] if found else "unknown"


def prune(builds, entry, previous):
    if output(entry, "alpha", "agent", "admitted") != "0":
        return
    candidates = [
        path for path in builds.iterdir()
        if re.fullmatch(SHA, path.name) and not path.is_symlink() and path.is_dir()
    ]
    pinned = (entry.resolve(), previous.resolve())
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    candidates.sort(key=lambda path: any(p.is_relative_to(path) for p in pinned),
                    reverse=True)
    for old in candidates[3:]:
        shutil.rmtree(old)


def install(revision, source, live, builds, log, lock):
    build = builds / revision
    entry = build / "bin/tbhbdev"
    verified = build / ".verified"
    old = live.resolve()
    already_live = old.is_relative_to(build)
    if build.is_symlink():
        raise ValueError("build directory is a symlink")
    if not already_live and not verified.is_file():
        # uv entry points contain absolute paths. Retry unfinished builds in place.
        if build.exists():
            shutil.rmtree(build)
        env = dict(
            os.environ, UV_TOOL_DIR=str(build), UV_TOOL_BIN_DIR=str(entry.parent)
        )
        # Keep the lock held if uv outlives a cancelled installer.
        subprocess.run(
            ["uv", "tool", "install", "--no-config", source, *extras(live)],
            env=env, stdout=log, stderr=log, check=True, pass_fds=(lock.fileno(),),
        )
    elif not already_live and extras(live) != extras(entry):
        raise ValueError("retained build has different extra requirements")
    if stamp(entry) != revision:
        raise ValueError("new entry point did not report the requested revision")
    subprocess.run(
        [build / "tbhbdev/bin/python", "-I", "-c", PROOF],
        stdout=log, stderr=log, check=True,
    )
    previous = builds / "previous"
    if not already_live:
        verified.touch()
        os.utime(build, None)
        live.parent.mkdir(parents=True, exist_ok=True)
        if old.is_file():
            replace_link(previous, old)
        replace_link(live, entry)
    try:
        prune(builds, entry, previous)
    except (OSError, subprocess.SubprocessError):
        traceback.print_exc(file=log)


def main(revision, source):
    started = time.monotonic()
    home = Path.home()
    live = home / ".local/bin/tbhbdev"
    builds = (home / ".local/share/tbhbdev/builds").resolve()
    log_path = home / ".local/state/tbhbdev/install-cli.log"
    before = after = "unknown"
    result = "failed"
    try:
        if not re.fullmatch(SHA, revision):
            raise ValueError("revision must be a full commit SHA")
        for key in ("PYTHONHOME", "PYTHONPATH"):
            os.environ.pop(key, None)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        builds.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as log, (builds / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            print(f"Installing {revision}", file=log, flush=True)
            try:
                before = after = stamp(live)
            except (OSError, subprocess.SubprocessError):
                traceback.print_exc(file=log)
            try:
                install(revision, source, live, builds, log, lock)
                after, result = revision, "ok"
            except Exception:
                traceback.print_exc(file=log)
    finally:
        print(f"{before} {after} {result} {time.monotonic() - started:.1f}s")
    return result != "ok"


sys.exit(main(*sys.argv[1:]))
'''


def script(revision: str, repo_url: str) -> str:
    """Return the standalone installer for the account that executes it."""
    source = f"git+{repo_url}@{revision}#subdirectory=factory/cli"
    args = shlex.join((revision, source))
    return (
        "python=python3\n"
        'if ! "$python" -I -c "import tomllib" >/dev/null 2>&1; then\n'
        '    python=$(uv python find --no-project --offline ">=3.11") ||\n'
        "        { echo 'unknown unknown failed 0.0s'; exit 1; }\n"
        "fi\n"
        f"exec \"$python\" -I - {args} <<'PY'\n{_INSTALL}PY\n"
    )
