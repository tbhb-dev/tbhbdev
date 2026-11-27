"""The CLI test I/O opt-out leaves the original operations available."""

import os
import socket
import subprocess
import sys
import time

import pytest
from bazel_test_io import block_io


def test_allows_connect() -> None:
    with socket.socket() as listener, socket.socket() as client:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        client.settimeout(1)
        client.connect(listener.getsockname())


def test_allows_subprocess() -> None:
    subprocess.run([sys.executable, "-c", "pass"], check=True)


def test_allows_sleep() -> None:
    time.sleep(0)


def test_guard_handles_missing_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "fork", raising=False)
    block_io(monkeypatch)

    assert not hasattr(os, "fork")
    with pytest.raises(RuntimeError, match=r"CLI unit tests.*time\.sleep"):
        time.sleep(0)


def test_allows_system() -> None:
    assert os.system("exit 0") == 0  # noqa: S605  # pyrefly: ignore[deprecated]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
@pytest.mark.parametrize("name", [name for name in dir(os) if name.startswith("exec")])
def test_allows_fork_and_exec(name: str) -> None:
    # Replace a child process so the test runner can check its exit status.
    path = sys.executable
    argv = [path, "-c", "raise SystemExit(7)"]
    args: list[object] = [path, *argv] if name.startswith("execl") else [path, argv]
    if name.endswith("e"):
        args.append(dict(os.environ))

    pid = os.fork()
    if pid == 0:
        try:
            getattr(os, name)(*args)
        finally:
            os._exit(127)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 7
