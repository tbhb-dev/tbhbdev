"""The local smoke port probe."""

from typing import Self

import pytest
from tbhbops_cli.o11y.ports import LocalPorts


def test_ports_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        def __init__(self) -> None:
            self.bound: list[tuple[str, int]] = []
            self.fail = False

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            self.bound.append(address)
            if self.fail:
                raise OSError

    socket = Socket()

    def socket_factory(_family: int, _kind: int) -> Socket:
        assert (_family, _kind) == (2, 1)
        return socket

    monkeypatch.setattr("tbhbops_cli.o11y.ports.socket.socket", socket_factory)
    assert LocalPorts().available(13000)
    socket.fail = True
    assert not LocalPorts().available(14317)
    assert socket.bound == [("127.0.0.1", 13000), ("127.0.0.1", 14317)]
