"""Read the bounded release stream and handle interrupted deployments."""

import os
import selectors
import signal
import sys
import time
from dataclasses import dataclass, field

from tbhbops.deploy.command import CommandError
from tbhbops.deploy.protocol import require


def stream(limit: int) -> bytes:
    """Wait for EOF for at most 300 seconds, with exactly one framed tar stream."""
    deadline = time.monotonic() + 300
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            require(
                remaining > 0 and bool(selector.select(remaining)), "stream deadline"
            )
            block = os.read(sys.stdin.fileno(), min(65536, limit + 1 - len(data)))
            if not block:
                return bytes(data)
            data.extend(block)
            require(len(data) <= limit, "stream limit")


@dataclass
class Signals:
    stopped: bool = False
    original: dict[int, signal._HANDLER] = field(default_factory=dict)

    def stop(self, _number: int, _frame: object) -> None:
        self.stopped = True

    def start(self) -> None:
        for number, handler in (
            (signal.SIGHUP, signal.SIG_IGN),
            (signal.SIGPIPE, signal.SIG_IGN),
            (signal.SIGTERM, self.stop),
            (signal.SIGINT, self.stop),
        ):
            self.original[number] = signal.signal(number, handler)

    def restore(self) -> None:
        for number, handler in self.original.items():
            signal.signal(number, handler)

    def check(self) -> None:
        if self.stopped:
            message = "stop signal"
            raise CommandError(message)
