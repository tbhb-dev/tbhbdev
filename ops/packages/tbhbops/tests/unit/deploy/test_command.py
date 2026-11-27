"""Test child command budgets and transient unit cleanup."""

import os
import signal
import subprocess
import sys
import time
from typing import BinaryIO, TypedDict, Unpack, override
from unittest.mock import Mock, call, patch

import pytest
from tbhbops.deploy import command as command_module
from tbhbops.deploy.command import Command, CommandError
from tbhbops.deploy.protocol import RefusalError


class ChildOptions(TypedDict, total=False, closed=True):
    stdin: BinaryIO
    stdout: int
    stderr: int
    env: dict[str, str]
    start_new_session: bool


@pytest.fixture(autouse=True)
def real_command_children_have_private_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen

    def spawn(
        argv: list[str], **options: Unpack[ChildOptions]
    ) -> subprocess.Popen[bytes]:
        child = original(argv, **options)
        if sys.platform != "win32":
            try:
                group = os.getpgid(child.pid)
            except ProcessLookupError:
                # A short-lived child may exit before the parent checks it.
                assert child.poll() is not None
            else:
                if group != child.pid:
                    # Kill the individual child before a bad group can hang a cap
                    # or timeout test. The test verifies the real kernel group.
                    child.kill()
                    child.communicate()
                    pytest.fail("command child has no private process group")
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)


def test_command_empty_environment() -> None:
    script = "import os,sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as popen:
        assert (
            Command()([sys.executable, "-S", "-c", script], b"input", timeout_s=1)
            == b"input"
        )
    assert popen.call_args.kwargs["env"] == {}
    assert popen.call_args.kwargs["start_new_session"] is True
    assert Command()(["/usr/bin/env"], timeout_s=1) == b""
    with pytest.raises(CommandError, match=r"^child command failed$"):
        Command()([sys.executable, "-c", "import sys; sys.exit(1)"], timeout_s=1)


@pytest.mark.parametrize("descriptor", [1, 2])
def test_command_caps_each_stream(descriptor: int) -> None:
    script = f"import os;\nwhile True: os.write({descriptor}, b'x' * 4096)"
    reason = "child stdout limit" if descriptor == 1 else "child stderr limit"
    started = time.monotonic()
    with pytest.raises(CommandError, match="^" + reason + "$"):
        Command()(
            [sys.executable, "-c", script],
            timeout_s=1,
            stdout_limit=1024,
            stderr_limit=2048,
        )
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("closed", [False, True])
def test_command_deadline_also_applies_after_output_closes(closed: bool) -> None:
    script = (
        "import os; os.close(1); os.close(2); " if closed else ""
    ) + "import time; time.sleep(10)"
    started = time.monotonic()
    with pytest.raises(CommandError, match=r"^child command timeout$"):
        Command()([sys.executable, "-c", script], timeout_s=0.1)
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("budget", ["cap", "timeout"])
def test_command_budget_stops_transient_unit(budget: str) -> None:
    stops: list[tuple[list[str], dict[str, object]]] = []

    class RecordingCommand(Command):
        @override
        def __call__(
            self,
            argv: list[str],
            data: bytes | None = None,
            *,
            timeout_s: float = 60,
            stdout_limit: int = 1024**2,
            stderr_limit: int = 1024**2,
            unit: str | None = None,
        ) -> bytes:
            if argv[0] == "/usr/bin/systemctl":
                stops.append(
                    (
                        argv,
                        {
                            "timeout_s": timeout_s,
                            "stdout_limit": stdout_limit,
                            "stderr_limit": stderr_limit,
                        },
                    )
                )
                return b"active\n" if len(stops) == 1 else b"inactive\n"
            return super().__call__(
                argv,
                data,
                timeout_s=timeout_s,
                stdout_limit=stdout_limit,
                stderr_limit=stderr_limit,
                unit=unit,
            )

    reason = "child stdout limit" if budget == "cap" else "child command timeout"
    script = (
        "print('too much')"
        if budget == "cap"
        else "import os,time; os.close(1); os.close(2); time.sleep(10)"
    )
    with pytest.raises(CommandError, match="^" + reason + "$"):
        RecordingCommand()(
            [sys.executable, "-c", script],
            stdout_limit=1,
            timeout_s=1 if budget == "cap" else 0.1,
            unit="tbhb-deploy-o11y",
        )
    assert stops == [
        (argv, {"timeout_s": 10, "stdout_limit": 1024**2, "stderr_limit": 1024**2})
        for argv in [
            [
                "/usr/bin/systemctl",
                "show",
                "--property=ActiveState",
                "--value",
                "tbhb-deploy-o11y",
            ],
            ["/usr/bin/systemctl", "stop", "tbhb-deploy-o11y"],
            [
                "/usr/bin/systemctl",
                "show",
                "--property=ActiveState",
                "--value",
                "tbhb-deploy-o11y",
            ],
        ]
    ]


def test_command_default_budgets_and_remaining_wait() -> None:
    with (
        patch.object(command_module.time, "monotonic", return_value=100),
        patch.object(command_module, "capture", return_value=b"result") as capture,
        patch.object(subprocess, "Popen") as popen,
    ):
        child = popen.return_value.__enter__.return_value
        child.returncode = 0
        assert Command()(["/command"]) == b"result"
    assert capture.call_args.args == (child.stdout, child.stderr, 160, 1024**2, 1024**2)
    child.wait.assert_called_once_with(timeout=60)
    assert popen.call_args.kwargs["stdout"] == subprocess.PIPE
    assert popen.call_args.kwargs["stderr"] == subprocess.PIPE
    assert popen.call_args.kwargs["start_new_session"] is True


def test_command_empty_stdin_and_independent_stream_boundaries() -> None:
    echo = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    ]
    assert Command()(echo, timeout_s=1) == b""
    assert Command()(echo, b"", timeout_s=1) == b""
    script = "import os; os.write(1,b'ab'); os.write(2,b'data')"
    assert (
        Command()(
            [sys.executable, "-c", script], stdout_limit=2, stderr_limit=4, timeout_s=1
        )
        == b"ab"
    )
    for stdout_limit, stderr_limit, reason in [
        (1, 4, "child stdout limit"),
        (2, 3, "child stderr limit"),
    ]:
        with pytest.raises(CommandError, match="^" + reason + "$"):
            Command()(
                [sys.executable, "-c", script],
                stdout_limit=stdout_limit,
                stderr_limit=stderr_limit,
                timeout_s=1,
            )


def test_read_output_never_requests_more_than_remaining_cap_and_one_byte() -> None:
    output = bytearray(b"ab")
    with patch.object(command_module.os, "read", side_effect=[b"cd", b"e"]) as read:
        assert command_module.read_output(11, output, 4, stdout=True)
        assert output == b"abcd"
        with pytest.raises(CommandError, match=r"^child stdout limit$"):
            command_module.read_output(11, output, 4, stdout=True)
    assert read.call_args_list == [call(11, 3), call(11, 1)]
    assert output == b"abcd"


def test_capture_refuses_at_deadline_even_if_output_is_ready() -> None:
    stdout, stderr = Mock(), Mock()
    stdout.fileno.return_value, stderr.fileno.return_value = 11, 12
    with (
        patch.object(command_module.time, "monotonic", return_value=100),
        patch.object(command_module.selectors, "DefaultSelector") as selector,
    ):
        selected = selector.return_value.__enter__.return_value
        closed: dict[int, object] = {}
        selected.get_map = Mock(side_effect=[{11: object()}, closed])
        selected.select = Mock(
            side_effect=AssertionError("expired deadline polled output")
        )
        with pytest.raises(CommandError, match=r"^child command timeout$"):
            command_module.capture(stdout, stderr, 100, 10, 10)
    selected.select.assert_not_called()


@pytest.mark.parametrize("failure", [CommandError, OSError])
def test_unit_stop_failure_blocks_compensation(
    failure: type[Exception],
) -> None:
    child = Mock(pid=123)
    command = Command()
    with (
        patch.object(command_module.os, "killpg") as kill,
        patch.object(Command, "__call__", side_effect=failure) as stop,
        pytest.raises(
            RefusalError,
            match=r"^transient unit stop unconfirmed; compensation withheld$",
        ),
    ):
        command.terminate(child, "tbhb-deploy-o11y")
    if sys.platform != "win32":
        kill.assert_called_once_with(123, signal.SIGKILL)
    child.wait.assert_called_once_with()
    stop.assert_called_once_with(
        [
            "/usr/bin/systemctl",
            "show",
            "--property=ActiveState",
            "--value",
            "tbhb-deploy-o11y",
        ],
        timeout_s=10,
    )


def test_read_output_reports_eof_without_extending_output() -> None:
    output = bytearray(b"before")
    with patch.object(command_module.os, "read", return_value=b""):
        assert command_module.read_output(11, output, 10, stdout=True) is False
    assert output == b"before"


def test_command_wait_never_extends_an_expired_budget() -> None:
    with (
        patch.object(command_module.time, "monotonic", side_effect=[100, 161]),
        patch.object(command_module, "capture", return_value=b"result"),
        patch.object(subprocess, "Popen") as popen,
    ):
        child = popen.return_value.__enter__.return_value
        child.returncode = 0
        assert Command()(["/command"]) == b"result"
    child.wait.assert_called_once_with(timeout=0)


def test_terminate_still_stops_unit_when_the_child_has_exited() -> None:
    child = Mock(pid=123)
    with (
        patch.object(command_module.os, "killpg", side_effect=ProcessLookupError),
        patch.object(Command, "__call__", return_value=b"inactive\n") as stop,
    ):
        Command().terminate(child, "tbhb-deploy-o11y")
    child.wait.assert_called_once_with()
    stop.assert_called_once_with(
        [
            "/usr/bin/systemctl",
            "show",
            "--property=ActiveState",
            "--value",
            "tbhb-deploy-o11y",
        ],
        timeout_s=10,
    )


@pytest.mark.parametrize("open_stream", [False, True])
def test_capture_unregisters_only_streams_that_reached_eof(open_stream: bool) -> None:
    stdout, stderr = Mock(), Mock()
    stdout.fileno.return_value, stderr.fileno.return_value = 11, 12
    key = Mock(fd=11)
    with (
        patch.object(command_module.time, "monotonic", return_value=100),
        patch.object(command_module.selectors, "DefaultSelector") as selector,
        patch.object(command_module, "read_output", return_value=open_stream),
    ):
        selected = selector.return_value.__enter__.return_value
        closed: dict[int, object] = {}
        selected.get_map = Mock(side_effect=[{11: object()}, closed])
        selected.select.return_value = [(key, 1)]
        assert command_module.capture(stdout, stderr, 101, 10, 10) == b""
    if open_stream:
        selected.unregister.assert_not_called()
    else:
        selected.unregister.assert_called_once_with(11)


@pytest.mark.parametrize(
    ("outputs", "reason"),
    [
        ([CommandError], "transient unit stop unconfirmed; compensation withheld"),
        ([OSError], "transient unit stop unconfirmed; compensation withheld"),
        (
            [b"active\n", CommandError],
            "transient unit stop unconfirmed; compensation withheld",
        ),
        (
            [b"active\n", b"", OSError],
            "transient unit stop unconfirmed; compensation withheld",
        ),
        (
            [b"active\n", b"", b"active\n"],
            "transient unit is not inactive; compensation withheld",
        ),
        (
            [b"active\n", b"", b"deactivating\n"],
            "transient unit is not inactive; compensation withheld",
        ),
        (
            [b"active\n", b"", b""],
            "transient unit is not inactive; compensation withheld",
        ),
    ],
)
def test_unit_stop_requires_confirmed_inactivity(
    outputs: list[bytes | type[Exception]], reason: str
) -> None:
    with (
        patch.object(Command, "__call__", side_effect=outputs),
        pytest.raises(RefusalError) as caught,
    ):
        Command().stop_unit("tbhb-deploy-o11y")
    assert str(caught.value) == reason


@pytest.mark.parametrize("active", [False, True])
def test_unit_stop_waits_with_bounded_commands(active: bool) -> None:
    unit = "tbhb-deploy-o11y"
    query = call(
        ["/usr/bin/systemctl", "show", "--property=ActiveState", "--value", unit],
        timeout_s=10,
    )
    outputs = [b"active\n", b"", b"inactive\n"] if active else [b"inactive\n"]
    with patch.object(Command, "__call__", side_effect=outputs) as command:
        Command().stop_unit(unit)
    expected = [query]
    if active:
        expected.extend(
            [
                call(["/usr/bin/systemctl", "stop", unit], timeout_s=10),
                query,
            ]
        )
    assert command.call_args_list == expected
