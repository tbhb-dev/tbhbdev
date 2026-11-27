"""Verify bounded input and signal handling."""

import os
import signal
import sys
from unittest.mock import Mock, call, patch

import pytest
from tbhbops.deploy import receiver_io
from tbhbops.deploy.command import CommandError
from tbhbops.deploy.protocol import RefusalError


def test_bounded_input(monkeypatch: pytest.MonkeyPatch) -> None:
    reader, writer = os.pipe()
    os.write(writer, b"input")
    os.close(writer)
    with os.fdopen(reader) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        assert receiver_io.stream(100) == b"input"
    reader, writer = os.pipe()
    os.write(writer, b"too much")
    os.close(writer)
    with os.fdopen(reader) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        with pytest.raises(RefusalError, match=r"^stream limit$"):
            receiver_io.stream(1)
    ticks = iter((0, 301))
    monkeypatch.setattr(receiver_io.time, "monotonic", lambda: next(ticks))
    reader, writer = os.pipe()
    os.close(writer)
    with os.fdopen(reader) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        with pytest.raises(RefusalError, match=r"^stream deadline$"):
            receiver_io.stream(100)


def test_signal_install_and_restore_exact_handlers() -> None:
    if sys.platform == "win32":
        pytest.skip("Unix signals required")
    signals = receiver_io.Signals()
    previous = [signal.SIG_DFL, signal.SIG_IGN, signal.SIG_DFL, signal.SIG_IGN]
    with patch.object(signal, "signal", side_effect=previous) as install:
        signals.start()
    assert install.call_args_list == [
        call(getattr(signal, "SIGHUP", 0), signal.SIG_IGN),
        call(getattr(signal, "SIGPIPE", 0), signal.SIG_IGN),
        call(signal.SIGTERM, signals.stop),
        call(signal.SIGINT, signals.stop),
    ]
    assert signals.original == dict(
        zip(
            (
                getattr(signal, "SIGHUP", 0),
                getattr(signal, "SIGPIPE", 0),
                signal.SIGTERM,
                signal.SIGINT,
            ),
            previous,
            strict=True,
        )
    )
    with patch.object(signal, "signal") as restore:
        signals.restore()
    assert restore.call_args_list == [
        call(number, handler) for number, handler in signals.original.items()
    ]
    signals.check()
    signals.stop(signal.SIGTERM, None)
    assert signals.stopped is True
    with pytest.raises(CommandError, match=r"^stop signal$"):
        signals.check()


@pytest.mark.parametrize("limit", [3, 100000])
def test_stream_selector_and_read_bounds(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    monkeypatch.setattr(receiver_io.sys, "stdin", Mock(fileno=Mock(return_value=31)))
    with (
        patch.object(receiver_io.selectors, "DefaultSelector") as selector_type,
        patch.object(receiver_io.time, "monotonic", side_effect=[10, 11, 12]),
        patch.object(receiver_io.os, "read", side_effect=[b"abc", b""]) as read,
    ):
        selector = selector_type.return_value.__enter__.return_value
        selector.select.return_value = [(31, receiver_io.selectors.EVENT_READ)]
        assert receiver_io.stream(limit) == b"abc"
    selector.register.assert_called_once_with(31, receiver_io.selectors.EVENT_READ)
    assert selector.select.call_args_list == [call(299), call(298)]
    assert read.call_args_list == [
        call(31, min(65536, limit + 1)),
        call(31, min(65536, limit - 2)),
    ]


@pytest.mark.parametrize("remaining", [0, -1, 1])
def test_stream_deadline_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, remaining: int
) -> None:
    monkeypatch.setattr(receiver_io.sys, "stdin", Mock(fileno=Mock(return_value=31)))
    with (
        patch.object(receiver_io.selectors, "DefaultSelector") as selector_type,
        patch.object(receiver_io.time, "monotonic", side_effect=[0, 300 - remaining]),
        patch.object(receiver_io.os, "read") as read,
    ):
        selector = selector_type.return_value.__enter__.return_value
        selector.select.return_value = ()
        with pytest.raises(RefusalError, match=r"^stream deadline$"):
            receiver_io.stream(3)
    read.assert_not_called()
    if remaining > 0:
        selector.select.assert_called_once_with(remaining)
    else:
        selector.select.assert_not_called()
