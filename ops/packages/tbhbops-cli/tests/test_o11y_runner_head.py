"""Bounded process prefixes stop reading and always terminate their producer."""

# cspell:ignore udcff

import io
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Self, override

import pytest
from tbhbops_cli.o11y import runner


class Stream(io.BytesIO):
    @override
    def fileno(self) -> int:
        return 42


@dataclass
class Process:
    stdout: Stream = field(default_factory=Stream)
    code: int = 17
    events: list[object] = field(default_factory=list)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.events.append(("exit", args))

    def wait(self, *, timeout: float) -> int:
        self.events.append(("wait", timeout))
        return self.code


def setup(monkeypatch: pytest.MonkeyPatch, content: bytes) -> Process:
    process = Process(Stream(content))

    def start(argv: list[str], **kwargs: object) -> Process:
        assert argv == ["producer", "logs"]
        assert kwargs == {
            "env": {"PATH": "/bin"},
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "start_new_session": True,
        }
        return process

    def ready(
        reads: list[io.BytesIO],
        writes: list[object],
        errors: list[object],
        timeout: float,
    ) -> tuple[list[io.BytesIO], list[object], list[object]]:
        assert reads == [process.stdout]
        assert writes == errors == []
        assert timeout == 116
        return reads, [], []

    def read(fd: int, size: int) -> bytes:
        assert fd == 42
        process.events.append(("read", size))
        return process.stdout.read(size)

    times = iter([10, *([14] * 100)])
    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner.select, "select", ready)
    monkeypatch.setattr(runner.os, "read", read)
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))

    def kill(_self: runner.ProcessHandle) -> None:
        process.events.append("kill")

    monkeypatch.setattr(runner.ProcessHandle, "kill", kill)
    return process


@pytest.mark.parametrize(
    ("content", "byte_limit", "line_limit", "expected", "reads"),
    [
        (b"one\ntwo\nthree\n", 100, 2, runner.Result(0, "one\ntwo\n"), [100]),
        (b"one\ntwo\n", 100, 2, runner.Result(0, "one\ntwo\n"), [100]),
        (
            b"one\rtwo\nthree\nfour",
            100,
            2,
            runner.Result(0, "one\rtwo\nthree\n"),
            [100],
        ),
        (b"one\rtwo", 100, 2, runner.Result(17, "one\rtwo"), [100, 93]),
        (b"", 100, 0, runner.Result(0), []),
        (b"x" * 200, 8, 200, runner.Result(0, "x" * 8), [8]),
        (b"a\xff\nlast", 100, 200, runner.Result(17, "a\udcff\nlast"), [100, 93]),
        (b"", 100, 200, runner.Result(17), [100]),
        (b"x" * 70001, 70000, 200, runner.Result(0, "x" * 70000), [65536, 4464]),
    ],
)
def test_read_head_contract(  # noqa: PLR0913, PLR0917  # table of independent stream limits
    content: bytes,
    byte_limit: int,
    line_limit: int,
    expected: runner.Result,
    reads: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = setup(monkeypatch, content)
    assert (
        runner.SubprocessRunner().read_head(
            ["producer", "logs"],
            {"PATH": "/bin"},
            120,
            byte_limit,
            line_limit,
        )
        == expected
    )
    assert [
        event[1]
        for event in process.events
        if isinstance(event, tuple) and event[0] == "read"
    ] == reads
    if expected.code == 17:
        assert ("wait", 116) in process.events
    assert process.events[-2:] == ["kill", ("exit", (None, None, None))]


@pytest.mark.parametrize("failure", ["idle", "deadline", "wait", "missing", "read"])
def test_read_head_errors(failure: str, monkeypatch: pytest.MonkeyPatch) -> None:
    process = setup(monkeypatch, b"")
    expected = runner.Result(None)
    if failure in ("idle", "deadline"):

        def idle(
            _reads: object, _writes: object, _errors: object, timeout: float
        ) -> tuple[list[object], list[object], list[object]]:
            assert timeout == (0 if failure == "deadline" else 116)
            return [], [], []

        monkeypatch.setattr(runner.select, "select", idle)
        if failure == "deadline":
            times = iter([10, 131])
            monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))
    elif failure == "wait":

        def wait(*, timeout: float) -> int:
            msg = "producer"
            raise subprocess.TimeoutExpired(msg, timeout)

        monkeypatch.setattr(process, "wait", wait)
    else:

        def missing(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(
            runner.subprocess if failure == "missing" else runner.os,
            "Popen" if failure == "missing" else "read",
            missing,
        )
        expected = runner.Result(127)
    assert (
        runner.SubprocessRunner().read_head(
            ["producer", "logs"], {"PATH": "/bin"}, 120, 100, 200
        )
        == expected
    )
    assert ("kill" in process.events) == (failure != "missing")


@pytest.mark.parametrize("lines", [False, True])
def test_read_head_real_large_producer(lines: bool) -> None:
    limit = 4 * 1024 * 1024
    payload = "b'line\\n'" if lines else "b'x' * 65536"
    script = f"import os\nwhile True: os.write(1, {payload})"
    result = runner.SubprocessRunner().read_head(
        [sys.executable, "-c", script], os.environ, 10, limit, 200
    )
    assert result == runner.Result(0, "line\n" * 200 if lines else "x" * limit)


def test_read_head_real_stderr_and_exit() -> None:
    result = runner.SubprocessRunner().read_head(
        [
            sys.executable,
            "-c",
            "import os; os.write(2, b'err\\xff'); raise SystemExit(23)",
        ],
        os.environ,
        10,
        100,
        200,
    )
    assert result == runner.Result(23, "err\udcff")


def test_read_head_requires_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = setup(monkeypatch, b"")
    monkeypatch.setattr(process, "stdout", None)
    with pytest.raises(AssertionError):
        runner.SubprocessRunner().read_head(
            ["producer", "logs"], {"PATH": "/bin"}, 120, 100, 200
        )
    assert process.events[0] == "kill"
    assert len(process.events) == 2
