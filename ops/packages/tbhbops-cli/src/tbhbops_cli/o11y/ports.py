"""Check loopback port availability without connecting to a service."""

import socket
from typing import Protocol


class Ports(Protocol):
    def available(self, port: int) -> bool: ...


class LocalPorts:
    def available(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True
