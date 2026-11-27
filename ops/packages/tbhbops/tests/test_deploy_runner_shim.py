"""Keep the job-started hook from leaving a key behind unless the guard admits."""

import contextlib
import importlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

# POSIX-only calls, which the tests that make them skip without.
POSIX_OS = importlib.import_module("os")
POSIX_SIGNAL = importlib.import_module("signal")

# The tests also run from a mutated copy of the package, so look upward.
ROLE = next(
    role
    for role in (
        parent / "ansible" / "roles" / "deploy_runner"
        for parent in Path(__file__).resolve().parents
    )
    if role.is_dir()
)
SHIM = ROLE / "files" / "job-guard.sh"
GUARD = ROLE / "files" / "deploy-runner-job-guard"
SLOT = ROLE.parent / "actions_runner_pool" / "files" / "actions-runner-slot.sh"
SECRETS = {"deploy-key": b"DEPLOY PRIVATE KEY\n", "repo-key": b"REPO PRIVATE KEY\n"}
BASH = shutil.which("bash") or "/bin/bash"
SH = shutil.which("sh") or "/bin/sh"
# The runner may run the hook under either, whatever the shebang says.
SHELLS = {
    "sh": [SH],
    "bash-e-pipefail": [BASH, "--noprofile", "--norc", "-e", "-o", "pipefail"],
}
if shutil.which("dash"):
    # What sh is on the pinned image.
    SHELLS["dash"] = [str(shutil.which("dash"))]
LITERALS = {
    "/etc/actions-runner/job-secrets": "fifo",
    "/usr/bin/python3": "python",
    "/etc/actions-runner/job/job-guard": "guard",
    "/run/job-secrets": "keys",
}
PRELUDE = f"""
import os, sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
loader = SourceFileLoader("g", {str(GUARD)!r})
guard = module_from_spec(spec_from_loader("g", loader))
loader.exec_module(guard)
KEYS = Path(os.environ["TEST_KEYS"])


def put(*names):
    for name in names:
        (KEYS / name).write_bytes(b"SECRET")
"""

shells = pytest.mark.parametrize("shell", SHELLS.values(), ids=SHELLS.keys())


def _writer_line() -> str:
    script = SLOT.read_text(encoding="utf-8")
    return next(row for row in script.splitlines() if row.strip().startswith("{ tar "))


class _Rig:
    """The hook's world in a temporary directory: literal paths replaced."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.source = tmp_path / "source"
        self.source.mkdir(mode=0o700)
        self.fifo = tmp_path / "fifo"
        self.keys = tmp_path / "keys"
        self.keys.mkdir(mode=0o700)
        self.guard = tmp_path / "guard.py"
        self.python = sys.executable
        self.shim = tmp_path / "job-guard.sh"
        self.writers: list[subprocess.Popen[bytes]] = []
        self.make_fifo()

    def make_fifo(self) -> None:
        POSIX_OS.mkfifo(self.fifo)

    def install(self, guard_source: str | None) -> None:
        if guard_source is not None:
            self.guard.write_text(PRELUDE + guard_source, encoding="utf-8")
        values = {
            "fifo": str(self.fifo),
            "python": self.python,
            "guard": str(self.guard),
            "keys": str(self.keys),
        }
        pattern = "|".join(re.escape(name) for name in sorted(LITERALS, key=len))
        text = re.sub(
            pattern,
            lambda match: values[LITERALS[match.group()]],
            SHIM.read_text(encoding="utf-8"),
        )
        self.shim.write_text(text, encoding="utf-8")

    def write_secrets(self, members: dict[str, bytes]) -> None:
        for name, data in members.items():
            (self.source / name).write_bytes(data)

    def start_writer(self) -> subprocess.Popen[bytes]:
        """The slot script's own writer line, blocked until a reader opens."""
        writer = subprocess.Popen(
            [BASH, "-c", f"set -euo pipefail\n{_writer_line()}\nwait"],
            env={
                **os.environ,
                "ACTIONS_RUNNER_POOL_JOB_SECRETS_DIR": str(self.source),
                "secrets_fifo": str(self.fifo),
            },
        )
        self.writers.append(writer)
        return writer

    def env(self, **overrides: str) -> dict[str, str]:
        env = {**os.environ, "TEST_KEYS": str(self.keys), "TMPDIR": str(self.root)}
        return env | overrides

    def run(
        self, shell: list[str], **overrides: str
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*shell, str(self.shim)],
            env=self.env(**overrides),
            capture_output=True,
            check=False,
            timeout=60,
        )

    def left(self) -> list[str]:
        return sorted(path.name for path in self.keys.iterdir())

    def close(self) -> None:
        for writer in self.writers:
            writer.kill()
            writer.wait()


@pytest.fixture
def rig(tmp_path: Path) -> Generator[_Rig]:
    world = _Rig(tmp_path)
    world.write_secrets(SECRETS)
    yield world
    world.close()


def _fifo_holds_nothing(path: Path) -> bool:
    descriptor = os.open(path, os.O_RDONLY | POSIX_OS.O_NONBLOCK)
    try:
        return os.read(descriptor, 65536) == b""
    except BlockingIOError:
        # A writer still holds it open.
        return False
    finally:
        os.close(descriptor)


@shells
def test_a_missing_python_leaves_no_key_and_the_fifo_empty(
    rig: _Rig, shell: list[str]
) -> None:
    rig.python = "/nonexistent/python3"
    rig.install(None)
    writer = rig.start_writer()
    # PATH names nothing, so the drain and the failure path run on builtins alone.
    result = rig.run(shell, PATH="/nonexistent")
    assert result.returncode == 1
    assert writer.wait(timeout=30) == 0
    assert _fifo_holds_nothing(rig.fifo)
    assert rig.left() == []


GUARD_WRITES_THEN_FAILS = """
put("deploy-key", "repo-key", ".hidden", "..dots", "dot.key")
sys.exit(1)
"""
GUARD_WRITES_THEN_DIES = """
put("deploy-key", "repo-key", ".hidden")
os.kill(os.getpid(), 9)
"""


@shells
@pytest.mark.parametrize("path", [None, "/nonexistent"], ids=["with-rm", "without-rm"])
@pytest.mark.parametrize(
    "source", [GUARD_WRITES_THEN_FAILS, GUARD_WRITES_THEN_DIES], ids=["exits", "killed"]
)
def test_a_guard_that_fails_after_writing_leaves_no_key(
    rig: _Rig, shell: list[str], source: str, path: str | None
) -> None:
    rig.install(source)
    writer = rig.start_writer()
    overrides = {} if path is None else {"PATH": path}
    result = rig.run(shell, **overrides)
    assert result.returncode == 1
    assert writer.wait(timeout=30) == 0
    if path is None:
        assert rig.left() == []
    else:
        # Without rm the contents still die: every file is truncated.
        assert rig.left() == sorted(
            name
            for name in (
                "deploy-key",
                "repo-key",
                ".hidden",
                "..dots",
                "dot.key",
            )
            if (rig.keys / name).exists()
        )
        assert rig.left()
        assert all((rig.keys / name).stat().st_size == 0 for name in rig.left())


GUARD_ADMITS = """
assert int(sys.argv[1]) == os.getppid(), (sys.argv, os.getppid())
members = guard.drain_secrets(sys.stdin.buffer)
for name, data in members.items():
    (KEYS / name).write_bytes(data)
"""


@shells
def test_the_shim_admits_when_the_guard_does_and_leaves_the_keys(
    rig: _Rig, shell: list[str]
) -> None:
    rig.install(GUARD_ADMITS)
    writer = rig.start_writer()
    result = rig.run(shell)
    assert result.returncode == 0, result.stderr
    assert writer.wait(timeout=30) == 0
    assert {name: (rig.keys / name).read_bytes() for name in rig.left()} == SECRETS


@shells
def test_the_shim_fails_without_a_fifo_and_never_runs_the_guard(
    rig: _Rig, shell: list[str]
) -> None:
    marker = rig.root / "ran"
    rig.install(f'Path({str(marker)!r}).write_text("ran")\n')
    rig.fifo.unlink()
    (rig.keys / "stale").write_bytes(b"x")
    result = rig.run(shell)
    # dash exits 2 on a failed exec redirect, the others 1.
    assert result.returncode in {1, 2}
    assert not marker.exists()


GUARD_DRAINS_THEN_ADMITS = """
try:
    members = guard.drain_secrets(sys.stdin.buffer)
except guard.RefusalError as error:
    sys.stderr.write(str(error))
    sys.exit(1)
for name, data in members.items():
    (KEYS / name).write_bytes(data)
"""


@shells
def test_a_stream_over_the_size_bound_is_refused_and_leaves_no_key(
    rig: _Rig, shell: list[str]
) -> None:
    rig.write_secrets({"padding": os.urandom(100 * 1024)})
    rig.install(GUARD_DRAINS_THEN_ADMITS)
    writer = rig.start_writer()
    result = rig.run(shell)
    assert result.returncode == 1
    assert b"longer than 65536 bytes" in result.stderr
    assert writer.wait(timeout=30) == 0
    assert rig.left() == []


@shells
def test_the_shim_hands_the_guard_the_whole_stream(rig: _Rig, shell: list[str]) -> None:
    # A large secrets dir within the bound, many lines of base64, joined intact.
    rig.write_secrets({"deploy-key": os.urandom(20 * 1024)})
    rig.install(GUARD_DRAINS_THEN_ADMITS)
    writer = rig.start_writer()
    result = rig.run(shell)
    assert result.returncode == 0, result.stderr
    assert writer.wait(timeout=30) == 0
    assert (rig.keys / "deploy-key").stat().st_size == 20 * 1024


def test_the_shim_uses_no_temporary_file_and_no_here_document() -> None:
    text = SHIM.read_text(encoding="utf-8")
    code = "\n".join(row for row in text.splitlines() if not row.startswith("#"))
    assert "<<" not in code
    assert "mktemp" not in code
    assert "/tmp" not in code  # noqa: S108 - the shim must not name a scratch path
    assert text.startswith("#!/bin/sh\n")


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the memory file, /proc and the death signal exist only on Linux",
)
@shells
def test_killing_the_shim_during_verification_leaves_no_key_anywhere(
    rig: _Rig, shell: list[str]
) -> None:
    report = rig.root / "report"
    cache = rig.root / "cache" / "repo.git"
    cache.mkdir(parents=True)
    settings = rig.root / "guard.json"
    settings.write_text(
        f'{{"repository": "tbhb-dev/tbhbdev", "ref": "refs/heads/main", '
        f'"events": ["push"], "url": "git@example.invalid:x.git", '
        f'"known_hosts": "/nonexistent", "cache": "{cache}"}}',
        encoding="utf-8",
    )
    rig.install(
        f"""
import re, time
def hold(argv, env):
    key = re.search(r"-i (\\S+)", env["GIT_SSH_COMMAND"]).group(1)
    Path({str(report)!r}).write_text(key)
    time.sleep(600)
guard._run = hold
sys.exit(guard.main(sys.argv, None, Path({str(settings)!r}), sys.stdin.buffer, KEYS))
"""
    )
    rig.write_secrets({})
    rig.start_writer()
    shim = subprocess.Popen(
        [*shell, str(rig.shim)],
        env=rig.env(
            GITHUB_REPOSITORY="tbhb-dev/tbhbdev",
            GITHUB_REF="refs/heads/main",
            GITHUB_EVENT_NAME="push",
            GITHUB_SHA="a" * 40,
        ),
    )
    try:
        deadline = time.monotonic() + 60
        while not report.exists() or not report.read_text(encoding="utf-8"):
            assert time.monotonic() < deadline, "the guard never reached the fetch"
            time.sleep(0.05)
        key_path = report.read_text(encoding="utf-8")
        match = re.fullmatch(r"/proc/(\d+)/fd/\d+", key_path)
        assert match
        guard_pid = int(match.group(1))
        # During verification the key is there, and only there.
        assert Path(key_path).read_bytes() == SECRETS["repo-key"]
        assert _files_holding(SECRETS["repo-key"], [rig.root, rig.keys]) == []
        assert rig.left() == []
    finally:
        shim.send_signal(POSIX_SIGNAL.SIGKILL)
        shim.wait(timeout=30)
    assert _gone(guard_pid)
    assert not Path(key_path).exists()
    assert rig.left() == []
    assert _files_holding(SECRETS["repo-key"], [rig.root, rig.keys]) == []
    assert _files_holding(SECRETS["deploy-key"], [rig.root, rig.keys]) == []


def _files_holding(needle: bytes, roots: list[Path]) -> list[Path]:
    """Regular files under the roots, other than the secrets source, with the bytes."""
    found = []
    for root in roots:
        for path in root.rglob("*"):
            if path.name == "source" or "source" in path.parts:
                continue
            if path.is_file() and not path.is_symlink():
                # A file that vanished or can't be read holds nothing to find.
                with contextlib.suppress(OSError):
                    if needle in path.read_bytes():
                        found.append(path)
    return found


def _gone(pid: int, wait: float = 30.0) -> bool:
    """Whether the process has exited in the time given; a zombie has."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            return True
        if "State:\tZ" in status:
            return True
        time.sleep(0.02)
    return False
