"""Bound child commands and confirm that failed transient units stop."""

import os
import selectors
import signal
import subprocess  # nosec B404: argv only, empty environment, no shell
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import BinaryIO, cast

from tbhbops.deploy.protocol import RefusalError, require

OUTPUT_LIMIT = 1024**2
# Chunk size only affects syscall count, never the byte cap or deadline.
READ_CHUNK = 65536  # pragma: no mutate


class CommandError(RuntimeError):
    """A child failed, with its potentially secret-bearing output withheld."""


def read_output(fd: int, output: bytearray, limit: int, *, stdout: bool) -> bool:
    block = os.read(fd, min(READ_CHUNK, limit + 1 - len(output)))
    if not block:
        return False
    if len(output) + len(block) > limit:
        message = "child stdout limit" if stdout else "child stderr limit"
        raise CommandError(message)
    output.extend(block)
    return True


def capture(
    stdout: BinaryIO,
    stderr: BinaryIO,
    deadline: float,
    stdout_limit: int,
    stderr_limit: int,
) -> bytes:
    outputs = {stdout.fileno(): bytearray(), stderr.fileno(): bytearray()}
    limits = {stdout.fileno(): stdout_limit, stderr.fileno(): stderr_limit}
    with selectors.DefaultSelector() as selector:
        selector.register(stdout, selectors.EVENT_READ)
        selector.register(stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            events = selector.select(remaining) if remaining > 0 else []
            if not events:
                message = "child command timeout"
                raise CommandError(message)
            for key, _ in events:
                if not read_output(
                    key.fd,
                    outputs[key.fd],
                    limits[key.fd],
                    stdout=key.fd == stdout.fileno(),
                ):
                    selector.unregister(key.fd)
    return bytes(outputs[stdout.fileno()])


@dataclass(frozen=True)
class Command:
    def __call__(  # Separate stream caps and unit cleanup are caller budgets.
        self,
        argv: list[str],
        data: bytes | None = None,
        *,
        timeout_s: float = 60,
        stdout_limit: int = OUTPUT_LIMIT,
        stderr_limit: int = OUTPUT_LIMIT,
        unit: str | None = None,
    ) -> bytes:
        deadline = time.monotonic() + timeout_s
        with tempfile.TemporaryFile() as source:
            source.write(data or b"")
            source.seek(0)
            with subprocess.Popen(  # nosec B603: argv only, no shell
                argv,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={},
                start_new_session=True,
            ) as process:
                try:
                    output = capture(
                        # PIPE guarantees both streams exist.
                        cast("BinaryIO", process.stdout),  # pragma: no mutate
                        cast("BinaryIO", process.stderr),  # pragma: no mutate
                        deadline,
                        stdout_limit,
                        stderr_limit,
                    )
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    self.terminate(process, unit)
                    message = "child command timeout"
                    raise CommandError(message) from None
                except CommandError:
                    self.terminate(process, unit)
                    raise
                if process.returncode:
                    message = "child command failed"
                    raise CommandError(message)
                return output

    def terminate(self, process: subprocess.Popen[bytes], unit: str | None) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        if unit is not None:
            self.stop_unit(unit)

    def stop_unit(self, unit: str) -> None:
        query = [
            "/usr/bin/systemctl",
            "show",
            "--property=ActiveState",
            "--value",
            unit,
        ]
        try:
            if self(query, timeout_s=10) == b"inactive\n":
                return
            self(["/usr/bin/systemctl", "stop", unit], timeout_s=10)
            state = self(query, timeout_s=10)
        except CommandError, OSError:
            message = "transient unit stop unconfirmed; compensation withheld"
            raise RefusalError(message) from None
        require(
            state == b"inactive\n",
            "transient unit is not inactive; compensation withheld",
        )
