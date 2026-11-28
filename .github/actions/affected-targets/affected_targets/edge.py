# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Git, process, download, and finish primitives for affected Bazel targets."""

import hashlib
import os
import platform
import secrets
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

from affected_targets import decide

TD_VERSION = "v0.34.1"
_AMD64 = "7f3360cf38d43d4d106440e8ce9d394d4b16bde52af11cd010e897394cec439b"
_ARM64 = "55d362d87b1c8d946b50ccc483d32ef48620ad1dd95eebd14e2ad41baf1baa2c"
TD_RELEASES = {
    ("Linux", "x86_64"): ("amd64", _AMD64),
    ("Linux", "aarch64"): ("arm64", _ARM64),
}
TD_URL = "https://github.com/bazel-contrib/target-determinator/releases/download/{}/{}"
STRIPPED_ENV = tuple(
    f"GITHUB_{name}" for name in ("OUTPUT", "STEP_SUMMARY", "ENV", "PATH", "STATE")
)


class FatalError(Exception):
    """Unrecoverable error that fails the selection step."""


def download(
    dest, *, opener=urllib.request.urlopen, system=None, machine=None, retries=3
):
    info = TD_RELEASES.get((system or platform.system(), machine or platform.machine()))
    if not info:
        return None
    arch, expected = info
    url = TD_URL.format(TD_VERSION, f"target-determinator.linux.{arch}")
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    part = target.with_name(target.name + ".part")
    for _ in range(retries + 1):
        try:
            digest = hashlib.sha256()
            with opener(url, timeout=60) as resp, part.open("wb") as handle:
                while chunk := resp.read(65536):
                    digest.update(chunk)
                    handle.write(chunk)
        except OSError:
            part.unlink(missing_ok=True)
            continue
        if digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            return None
        part.chmod(0o755)
        part.replace(target)
        return target
    return None


def stripped_env(env):
    return {key: value for key, value in env.items() if key not in STRIPPED_ENV}


def show(message, stream):
    stream.write(decide.stopped(message, secrets.token_hex(16)))
    stream.flush()


def _killpg(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    return True


def deadline(
    cmd, seconds, *, grace=60.0, reap_polls=50, stdout=subprocess.PIPE, **popen_kw
):
    if seconds <= 0:
        return 124, b""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=stdout, start_new_session=True, **popen_kw
    )
    expired, out = False, b""
    try:
        try:
            out, _ = proc.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            expired = True
            _killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                _killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    finally:
        _killpg(proc.pid, signal.SIGKILL)
        for _ in range(reap_polls):
            if not _killpg(proc.pid, 0):
                break
            time.sleep(0.1)
        if proc.stdout is not None:
            proc.stdout.close()
    if expired:
        return 124, b""
    rc = proc.returncode
    return (128 - rc if rc < 0 else rc), out or b""


def git(*args, cwd, env=None, stderr=subprocess.DEVNULL):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
        check=False,
    )


def clean(cwd, env):
    st = git(
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=none",
        cwd=cwd,
        env=env,
    )
    if st.returncode or st.stdout:
        return False
    ls = git("ls-files", "-v", "--recurse-submodules", ":/", cwd=cwd, env=env)
    if ls.returncode:
        return False
    return not any(
        line[:1] == "S" or "a" <= line[:1] <= "z"
        for line in os.fsdecode(ls.stdout).splitlines()
    )


def at_head(want, *, ran, cwd, env):
    res = git("rev-parse", "HEAD", cwd=cwd, env=env)
    if res.returncode or os.fsdecode(res.stdout).rstrip("\n") != want:
        return False
    return clean(cwd, env) if ran else True


def ensure_head(want, *, ran, cwd, env, stderr):
    if at_head(want, ran=ran, cwd=cwd, env=env):
        return True
    stderr.write(f"affected-targets: checking out the event's head {want}\n")
    stderr.flush()
    cmds = [["checkout", "-q", *(["-f"] if ran else []), "--detach", want]]
    if ran:
        cmds.append(["submodule", "update", "-q", "--recursive", "--force"])
    for args in cmds:
        res = git(*args, cwd=cwd, env=env, stderr=subprocess.STDOUT)
        show(res.stdout.decode("utf-8", "replace"), stderr)
        if res.returncode:
            return False
    return at_head(want, ran=ran, cwd=cwd, env=env)


def finish(mode, reason, want, target_file, *, ran, cwd, env, stdout, stderr):
    if mode != "all" and not at_head(want, ran=ran, cwd=cwd, env=env):
        mode, reason = "all", "the checkout changed while the tools ran"
    reason = decide.safe_reason(reason)
    if not ensure_head(want, ran=ran, cwd=cwd, env=env, stderr=stderr):
        msg = f"can't restore a clean checkout of the event's head {want}"
        raise FatalError(msg)
    if mode == "all":
        targets, count = decide.fallback(), "all"
        target_file.write_text(targets, encoding="utf-8")
    else:
        selected = [
            line
            for line in target_file.read_text(encoding="utf-8").splitlines()
            if line
        ]
        targets, count = decide.lines(selected), str(len(selected))
    delim = f"EOF_{secrets.token_hex(16)}"
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
        handle.write(decide.output_text(count, reason, target_file, targets, delim))
    if summary := env.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(decide.summary_text(count, reason))
    show(f"affected-targets: {count}: {reason}", stdout)
