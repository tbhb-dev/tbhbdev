"""Bounded process execution shared by observability commands."""

import contextlib
import io
import os
import select
import signal
import subprocess  # nosec B404: this seam runs explicit argument vectors
import time
from dataclasses import dataclass
from typing import Protocol
lazy from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class Result:
    code: int | None
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result: ...

    def read_head(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        byte_limit: int,
        line_limit: int,
    ) -> Result: ...


class ProcessHandle:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        try:
            # Arguments and environment are supplied explicitly through the seam.
            result = subprocess.run(  # nosec B603
                argv,
                env=env,
                timeout=timeout,
                input=input_data,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Result(None)
        except OSError:
            return Result(127)
        return Result(result.returncode, result.stdout, result.stderr)

    def read_head(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        byte_limit: int,
        line_limit: int,
    ) -> Result:
        """Capture a bounded prefix, merging diagnostics, then reap the process."""
        try:
            with subprocess.Popen(  # nosec B603
                argv,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            ) as process:
                try:
                    return _read_head(process, timeout, byte_limit, line_limit)
                finally:
                    ProcessHandle(process).kill()
        except subprocess.TimeoutExpired:
            return Result(None)
        except OSError:
            return Result(127)


def _read_head(
    process: subprocess.Popen[bytes],
    timeout: int,
    byte_limit: int,
    line_limit: int,
) -> Result:
    assert process.stdout is not None  # nosec B101: PIPE guarantees a stream; narrow its type
    stream = process.stdout
    deadline = time.monotonic() + timeout
    data = bytearray()
    code = 0
    while len(data) < byte_limit and data.count(b"\n") < line_limit:
        remaining = max(0, deadline - time.monotonic())
        if not select.select([stream], [], [], remaining)[0]:
            return Result(None)
        chunk = os.read(stream.fileno(), min(65536, byte_limit - len(data)))
        if not chunk:
            code = process.wait(timeout=remaining)
            break
        data.extend(chunk)
    prefix = b"".join(io.BytesIO(data).readlines()[:line_limit])
    return Result(code, prefix.decode(errors="surrogateescape"))
