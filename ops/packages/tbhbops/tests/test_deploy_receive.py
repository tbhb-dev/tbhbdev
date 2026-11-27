"""End-to-end receiver states with real transactions and a simulated daemon."""

import io
import json
import os
import signal
import sys
from dataclasses import replace
from typing import override
from unittest.mock import Mock, call, patch
lazy from pathlib import Path

import pytest
from tbhbops.deploy import compose, receive
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.command import CommandError
from tbhbops.deploy.compose import Engine
from tbhbops.deploy.protocol import Json, RefusalError, Request, decode, mapping, pack
from tbhbops.deploy.transaction import Lock, Transaction
lazy from tbhbops.deploy.config import Service

from .deploy_fakes import FakeCommand
from .deploy_model import minimal
from .test_deploy_config import deploy_service as deploy_service  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_filesystem import trusted_tree as trusted_tree  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_protocol import chained_pax

RELEASE = "a" * 40
PREVIOUS = "b" * 40


@pytest.fixture
def capability_receiver(
    receiver: receive.Receiver, monkeypatch: pytest.MonkeyPatch
) -> receive.Receiver:
    # The isolated Compose parser supplies a valid model; the daemon read and
    # every receiver operation use the real preflight and transaction paths.
    monkeypatch.setattr(receiver.engine, "sandbox", Mock(return_value=minimal()))
    return receiver


def refused_capability(receiver: receive.Receiver, reason: str) -> None:
    before = sorted(receiver.service.root.rglob("*"))
    assert receiver.run() == 2
    assert result(receiver)["outcome"] == "refused"
    assert result(receiver)["step"] == "capabilities"
    assert result(receiver)["reason"] == reason
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    assert command.calls == [
        (["/usr/bin/docker", "info", "--format", "{{json .}}"], None)
    ]
    assert command.budgets == [
        {"timeout_s": 10, "stdout_limit": 1024**2, "stderr_limit": 4096}
    ]
    assert engine(receiver).events == []
    assert sorted(receiver.service.root.rglob("*")) == before


def test_refuses_release_when_daemon_cannot_enforce_pids_limit(
    capability_receiver: receive.Receiver,
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    info = json.loads(command.info)
    info.update(Driver="btrfs", ServerVersion="24.0.2", PidsLimit=False)
    command.info = json.dumps(info).encode()
    refused_capability(
        capability_receiver, "daemon cannot enforce PidsLimit for service web"
    )


def test_accepts_release_when_daemon_enforces_required_limits(
    capability_receiver: receive.Receiver,
) -> None:
    assert capability_receiver.run() == 0
    assert result(capability_receiver)["outcome"] == "ok"
    assert "up " + RELEASE in engine(capability_receiver).events
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    assert command.calls[0] == (
        ["/usr/bin/docker", "info", "--format", "{{json .}}"],
        None,
    )
    assert (
        fs.target(capability_receiver.service.root / "current") == "releases/" + RELEASE
    )


def test_capability_refusal_preserves_pending_recovery(
    capability_receiver: receive.Receiver,
) -> None:
    seed_previous(capability_receiver)
    transaction = Transaction(capability_receiver.service, capability_receiver.engine)
    transaction.save(RELEASE)
    transaction.write({"token": b"interrupted"})
    before = transaction.journal()
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    info = json.loads(command.info)
    info["PidsLimit"] = False
    command.info = json.dumps(info).encode()
    refused_capability(
        capability_receiver, "daemon cannot enforce PidsLimit for service web"
    )
    assert transaction.journal() == before
    assert capability_receiver.service.secrets[0].target.read_bytes() == b"interrupted"


@pytest.mark.parametrize(
    "failure", [CommandError, OSError, ValueError, KeyError, TypeError]
)
def test_preflight_failure_withholds_exception_output(
    receiver: receive.Receiver, failure: type[Exception]
) -> None:
    with patch.object(
        receiver.engine, "preflight", side_effect=failure("private output")
    ):
        assert receiver.run() == 2
    assert result(receiver)["reason"] == "receiver step failed"
    assert result(receiver)["outcome"] == "refused"
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    assert command.calls == []
    assert engine(receiver).events == []


def test_refuses_release_when_daemon_cannot_enforce_memory_limit(
    capability_receiver: receive.Receiver,
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    info = json.loads(command.info)
    info["MemoryLimit"] = False
    command.info = json.dumps(info).encode()
    refused_capability(
        capability_receiver, "daemon cannot enforce MemoryLimit for service web"
    )


@pytest.mark.parametrize("required", [False, True])
def test_swap_capability_is_required_only_for_memswap_limit(
    capability_receiver: receive.Receiver, required: bool
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    info = json.loads(command.info)
    info["SwapLimit"] = False
    command.info = json.dumps(info).encode()
    parser = capability_receiver.engine.sandbox
    assert isinstance(parser, Mock)
    parser.return_value = minimal(**({"memswap_limit": "2048"} if required else {}))
    if required:
        refused_capability(
            capability_receiver, "daemon cannot enforce SwapLimit for service web"
        )
    else:
        assert capability_receiver.run() == 0
        assert result(capability_receiver)["outcome"] == "ok"


@pytest.mark.parametrize("key", ["Driver", "ServerVersion"])
@pytest.mark.parametrize("value", ["", "  ", None, False, 29])
def test_refuses_unreachable_daemon_before_testing_limits(
    capability_receiver: receive.Receiver, key: str, value: Json
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    info = json.loads(command.info)
    info.update(PidsLimit=False, MemoryLimit=False, SwapLimit=False)
    info[key] = value
    command.info = json.dumps(info).encode()
    refused_capability(capability_receiver, "daemon unreachable")


@pytest.mark.parametrize("key", ["PidsLimit", "MemoryLimit", "SwapLimit"])
@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_refuses_non_boolean_daemon_capability(
    capability_receiver: receive.Receiver, key: str, value: Json
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    info = json.loads(command.info)
    info[key] = value
    command.info = json.dumps(info).encode()
    refused_capability(capability_receiver, "daemon capability report invalid: " + key)


@pytest.mark.parametrize("key", ["PidsLimit", "MemoryLimit", "SwapLimit"])
def test_refuses_missing_daemon_capability(
    capability_receiver: receive.Receiver, key: str
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    info = json.loads(command.info)
    del info[key]
    command.info = json.dumps(info).encode()
    refused_capability(capability_receiver, "daemon capability report invalid: " + key)


@pytest.mark.parametrize(
    "body", [b"private output", b"[]", b"null", b"\xff", b'{"Driver":1,"Driver":2}']
)
def test_refuses_invalid_daemon_report_without_echoing_output(
    capability_receiver: receive.Receiver, body: bytes
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    command.info = body
    refused_capability(capability_receiver, "daemon capability report unavailable")


@pytest.mark.parametrize(
    "failure",
    [
        CommandError("child command timeout"),
        CommandError("child command failed"),
        OSError("private output"),
    ],
    ids=["timeout", "nonzero-exit", "os-error"],
)
def test_refuses_failed_daemon_read_without_echoing_output(
    capability_receiver: receive.Receiver, failure: Exception
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    command.info_error = failure
    refused_capability(capability_receiver, "daemon capability report unavailable")


class ReceiveEngine(Engine):
    def __init__(self, service: Service) -> None:
        super().__init__(service, "/compose", FakeCommand())
        self.events: list[str] = []
        self.fail_at: set[str] = set()

    def event(self, name: str) -> None:
        self.events.append(name)
        if name in self.fail_at:
            raise CommandError(name)

    @override
    def parse(self, release: str) -> None:
        self.event("parse")
        fs.write(self.model_path(release), b"{}")

    @override
    def stored(self, release: str) -> Json:
        return {}

    @override
    def stop(self) -> None:
        self.event("stop")

    @override
    def stop_unit(self) -> None:
        self.event("stop-unit")

    @override
    def check_paths(
        self, candidate: Json, release: str, *, missing: bool = False, live: bool = True
    ) -> None:
        self.event("paths " + release)

    @override
    def run(self, release: str, *arguments: str, timeout_s: float = 300) -> None:
        self.event(arguments[0] + " " + release)
        if "--force-recreate" in arguments:
            self.events.append("force-recreate")

    @override
    def local_images(self, release: str) -> bool:
        return True

    @override
    def health(self, release: str) -> None:
        self.event("health " + release)


@pytest.fixture
def receiver(
    deploy_service: Service, trusted_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> receive.Receiver:
    root = trusted_tree
    target = root / "data" / "secrets" / "token"
    target.parent.mkdir(parents=True, mode=0o700)
    service = replace(
        deploy_service,
        root=root,
        init=(),
        secrets=(replace(deploy_service.secrets[0], target=target),),
        min_free_gib=0,
    )
    wanted = Request("o11y", RELEASE, "123")
    body = pack(
        wanted,
        {"release/compose.yaml": b"model", "release/config/web": b"config"},
        {"token": b"fixture"},
    )

    def read_stream(_limit: int) -> bytes:
        return body

    monkeypatch.setattr(receive, "stream", read_stream)
    return receive.Receiver(wanted, service, ReceiveEngine(service), io.StringIO())


def engine(receiver: receive.Receiver) -> ReceiveEngine:
    assert isinstance(receiver.engine, ReceiveEngine)
    return receiver.engine


def result(receiver: receive.Receiver) -> dict[str, Json]:
    assert isinstance(receiver.out, io.StringIO)
    return mapping(decode(receiver.out.getvalue().splitlines()[-1].encode()))


def align_secret_group(receiver: receive.Receiver) -> None:
    secret = receiver.service.secrets[0]
    receiver.service = replace(
        receiver.service,
        secrets=(replace(secret, gid=secret.target.parent.stat().st_gid),),
    )
    receiver.engine.service = receiver.service


def seed_previous(receiver: receive.Receiver) -> None:
    receiver.prepare()
    (receiver.service.root / "releases" / PREVIOUS).mkdir()
    fs.pointer(receiver.service.root / "current", "releases/" + PREVIOUS)
    fs.write(receiver.service.secrets[0].target, b"earlier")


@pytest.mark.parametrize("extra", [0, 1])
def test_retained_bytes_boundary(receiver: receive.Receiver, extra: int) -> None:
    seed_previous(receiver)
    receiver.service = replace(receiver.service, max_retained_mib=1)
    previous = receiver.service.root / "releases" / PREVIOUS
    (previous / "nested").mkdir()
    # Candidate: model (5), config (6), and the SHA-256 marker (64).
    (previous / "nested" / "data").write_bytes(b"x" * (1024**2 - 76 + extra))
    (previous / "other").write_bytes(b"x")
    if extra:
        assert receiver.run() == 2
        assert result(receiver)["outcome"] == "refused"
        assert result(receiver)["reason"] == "retained bytes limit"
        assert result(receiver)["step"] == "release"
        assert not (receiver.service.root / "releases" / RELEASE).exists()
        assert not list((receiver.service.root / "incoming").iterdir())
        assert not (receiver.service.root / "deploy" / "txn").exists()
        assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
        assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
    else:
        assert receiver.run() == 0
        assert result(receiver)["outcome"] == "ok"
        # A retry at the ceiling counts the existing release only once.
        assert receiver.run() == 0
        assert result(receiver)["outcome"] == "ok"
    assert (previous / "other").read_bytes() == b"x"


@pytest.mark.parametrize("extra", [0, 1])
def test_payload_bytes_boundary(receiver: receive.Receiver, extra: int) -> None:
    receiver.service = replace(receiver.service, max_stream_mib=1)
    files = {"release/compose.yaml": b""}
    secrets = {"token": b"fixture"}
    overhead = len(pack(receiver.wanted, files, secrets))
    files["release/compose.yaml"] = bytes(1024**2 - overhead)
    body = pack(receiver.wanted, files, secrets)
    assert len(body) == 1024**2
    with patch.object(receive, "stream", return_value=body + bytes(extra)) as stream:
        assert receiver.run() == (2 if extra else 0)
    stream.assert_called_once_with(1024**2)
    if extra:
        assert result(receiver)["reason"] == "stream limit"
        assert result(receiver)["outcome"] == "refused"
        assert engine(receiver).events == []
        assert not (receiver.service.root / "releases").exists()
    else:
        assert result(receiver)["outcome"] == "ok"


def test_success_and_identical_release(receiver: receive.Receiver) -> None:
    assert receiver.run() == 0
    assert result(receiver)["outcome"] == "ok"
    assert result(receiver)["step"] == "done"
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"
    assert not (receiver.service.root / "deploy" / "txn").exists()
    assert "force-recreate" in engine(receiver).events
    receiver.incoming = None
    receiver.created = False
    assert receiver.run() == 0
    assert (receiver.service.root / "deploy" / "lock").is_file()


def test_release_conflict_and_policy_cleanup(receiver: receive.Receiver) -> None:
    engine(receiver).fail_at = {"parse"}
    assert receiver.run() == 2
    assert result(receiver)["step"] == "policy"
    assert not (receiver.service.root / "releases" / RELEASE).exists()
    assert not receiver.service.secrets[0].target.exists()
    engine(receiver).fail_at.clear()
    receiver.created = False
    assert receiver.run() == 0
    fs.write(receiver.service.root / "releases" / RELEASE / ".sha256", b"other")
    receiver.created = False
    assert receiver.run() == 2
    assert result(receiver)["step"] == "release"


@pytest.mark.parametrize(
    "failure", ["pull " + RELEASE, "up " + RELEASE, "health " + RELEASE]
)
def test_compensation(receiver: receive.Receiver, failure: str) -> None:
    seed_previous(receiver)
    engine(receiver).fail_at = {failure}
    code = receiver.run()
    assert code == (4 if failure.startswith("pull") else 1)
    assert result(receiver)["outcome"] == ("aborted" if code == 4 else "rolled_back")
    assert result(receiver)["reason"] == "deployment compensated"
    assert result(receiver)["step"] == ("pull" if code == 4 else "rollback")
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    assert not (receiver.service.root / "deploy" / "txn").exists()
    if not failure.startswith("pull"):
        assert engine(receiver).events.index("stop") < engine(receiver).events.index(
            "paths " + PREVIOUS
        )


def test_no_prior_release_and_failed_rollback(receiver: receive.Receiver) -> None:
    engine(receiver).fail_at = {"up " + RELEASE}
    assert receiver.run() == 3
    assert result(receiver)["outcome"] == "failed"
    assert fs.target(receiver.service.root / "current") is None
    assert not receiver.service.secrets[0].target.exists()
    receiver.created = False
    seed_previous(receiver)
    engine(receiver).fail_at.add("health " + PREVIOUS)
    assert receiver.run() == 3
    assert (receiver.service.root / "deploy" / "txn" / "journal.json").exists()
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS


def test_manual_rollback_and_recovery_only(receiver: receive.Receiver) -> None:
    seed_previous(receiver)
    assert receiver.run() == 0
    receiver.created = False
    assert receiver.run(manual=True) == 0
    assert receiver.wanted == Request("o11y", PREVIOUS, None)
    assert result(receiver)["stack"] == "o11y"
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save(RELEASE)
    transaction.write({"token": b"interrupted"})
    assert receiver.run(manual=True) == 0
    assert result(receiver)["outcome"] == "ok"
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"


def test_busy_space_and_no_previous(receiver: receive.Receiver) -> None:
    receiver.prepare()
    lock = Lock(receiver.service.root / "deploy")
    lock.acquire()
    assert receiver.run() == 75
    assert result(receiver)["outcome"] == "busy"
    assert result(receiver)["reason"] == "live deploy lock"
    lock.release()
    receiver.service = replace(receiver.service, min_free_gib=2**40)
    assert receiver.run() == 2
    assert isinstance(receiver.out, io.StringIO)
    assert result(receiver)["step"] == "space"
    assert receiver.run(manual=True) == 2


def test_signal_compensates(
    receiver: receive.Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = receiver.engine.run

    def interrupted(release: str, *args: str) -> None:
        original(release, *args)
        receiver.signals.stop(signal.SIGTERM, None)

    monkeypatch.setattr(receiver.engine, "run", interrupted)
    assert receiver.run() == 4
    assert not receiver.service.secrets[0].target.exists()


def test_broken_output_does_not_interrupt_receiver(receiver: receive.Receiver) -> None:
    class Broken(io.StringIO):
        @override
        def write(self, _body: str) -> int:
            raise BrokenPipeError

    receiver.out = Broken()
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    command.failed = True
    receiver.line("ok")


def test_bounded_input(monkeypatch: pytest.MonkeyPatch) -> None:
    reader, writer = os.pipe()
    os.write(writer, b"input")
    os.close(writer)
    with os.fdopen(reader) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        assert receive.stream(100) == b"input"
    reader, writer = os.pipe()
    os.write(writer, b"too much")
    os.close(writer)
    with os.fdopen(reader) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        with pytest.raises(RefusalError, match=r"^stream limit$"):
            receive.stream(1)
    ticks = iter((0, 301))
    monkeypatch.setattr(receive.time, "monotonic", lambda: next(ticks))
    reader, writer = os.pipe()
    os.close(writer)
    with os.fdopen(reader) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        with pytest.raises(RefusalError, match=r"^stream deadline$"):
            receive.stream(100)


def test_request_identity_and_version(
    deploy_service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE)

    def load(path: Path) -> Json:
        return (
            {"compose_binary": "/compose", "compose_version": "5.7.1"}
            if path.name == "receiver.toml"
            else {}
        )

    monkeypatch.setattr(receive, "load", load)
    monkeypatch.setattr(receive, "verify_binary", Mock())

    def allowed(_value: Json, _name: str) -> Service:
        return deploy_service

    def version(
        _self: receive.Command,
        _args: list[str],
        _data: bytes | None = None,
        **_kwargs: object,
    ) -> bytes:
        return b"5.7.1\n"

    def run_receiver(_self: receive.Receiver, *, manual: bool = False) -> int:
        return 7 if manual else 0

    monkeypatch.setattr(receive, "service", allowed)
    monkeypatch.setattr(receive.Command, "__call__", version)
    monkeypatch.setattr(receive.Receiver, "run", run_receiver)
    assert receive.receive(io.StringIO()) == 0
    assert receive.receive(io.StringIO(), rollback="o11y") == 2
    monkeypatch.setenv("SUDO_USER", "operator")
    assert receive.receive(io.StringIO(), rollback="o11y") == 7
    assert receive.receive(io.StringIO()) == 2
    assert receive.receive(io.StringIO(), rollback="bad name") == 2
    monkeypatch.setenv("SUDO_USER", "deploy")

    def wrong_version(
        _self: receive.Command,
        _args: list[str],
        _data: bytes | None = None,
        **_kwargs: object,
    ) -> bytes:
        return b"5.7.0\n"

    monkeypatch.setattr(receive.Command, "__call__", wrong_version)
    output = io.StringIO()
    assert receive.receive(output) == 2
    assert json.loads(output.getvalue())["reason"] == "Compose version mismatch"
    monkeypatch.setattr(os, "geteuid", lambda: 1)
    output = io.StringIO()
    assert receive.receive(output) == 2
    assert json.loads(output.getvalue())["release"] is None


def test_init_and_unchanged_secret_skip_recreate(receiver: receive.Receiver) -> None:
    executable = receiver.service.root / "init"
    executable.write_bytes(b"fixture")
    receiver.service = replace(receiver.service, init=(str(executable),))
    assert receiver.run() == 0
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    assert ([str(executable)], None) in command.calls
    receiver.incoming = None
    receiver.created = False
    # Align the test owner's group with the trusted os.stat adapter.
    secret = receiver.service.secrets[0]
    receiver.service = replace(
        receiver.service, secrets=(replace(secret, gid=secret.target.stat().st_gid),)
    )
    engine(receiver).events.clear()
    assert receiver.run() == 0
    assert "force-recreate" not in engine(receiver).events
    marker = receiver.service.root / "deploy" / "force-recreate"
    fs.write(marker, b"1\n")
    receiver.created = False
    receiver.incoming = None
    assert receiver.run() == 0
    assert "force-recreate" in engine(receiver).events
    assert not marker.exists()


def test_recover_then_continue_deploy(receiver: receive.Receiver) -> None:
    receiver.prepare()
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save(PREVIOUS)
    transaction.write({"token": b"interrupted"})
    assert receiver.run() == 0
    assert isinstance(receiver.out, io.StringIO)
    assert '"outcome": "recovered"' in receiver.out.getvalue()
    assert result(receiver)["outcome"] == "ok"


@pytest.mark.parametrize("reason", [None, "safe refusal"])
def test_exact_result_record(
    receiver: receive.Receiver, monkeypatch: pytest.MonkeyPatch, reason: str | None
) -> None:
    receiver.started = 100
    receiver.step = "health"
    monkeypatch.setattr(receive.time, "monotonic", lambda: 102.36)
    fake = receiver.engine.command
    assert isinstance(fake, FakeCommand)
    expected: dict[str, Json] = {
        "stack": "o11y",
        "release": RELEASE,
        "outcome": "failed",
        "step": "health",
        "duration_s": 2.4,
        "run_id": "123",
    }
    if reason is not None:
        expected["reason"] = reason
    body = json.dumps(expected, sort_keys=True) + "\n"
    with patch.object(receiver.out, "flush", wraps=receiver.out.flush) as flush:
        receiver.line("failed", reason)
    assert isinstance(receiver.out, io.StringIO)
    assert receiver.out.getvalue() == body
    assert fake.calls == [
        (["/usr/bin/systemd-cat", "--identifier=tbhb-deploy-receive"], body.encode())
    ]
    flush.assert_called_once_with()


def test_signal_install_and_restore_exact_handlers() -> None:
    if sys.platform == "win32":
        pytest.skip("Unix signals required")
    signals = receive.Signals()
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


@pytest.mark.parametrize(
    "root_info",
    [
        None,
        os.stat_result((0o100700, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
        os.stat_result((0o040700, 0, 0, 0, 1, 0, 0, 0, 0, 0)),
        os.stat_result((0o040755, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    ],
)
def test_prepare_refuses_root_before_mkdir(
    receiver: receive.Receiver, root_info: os.stat_result | None
) -> None:
    with (
        patch.object(fs, "inspect", return_value=root_info),
        patch.object(fs, "mkdir") as mkdir,
        pytest.raises(RefusalError, match=r"^service root ownership or mode$"),
    ):
        receiver.prepare()
    mkdir.assert_not_called()


def test_stage_exact_files_and_modes(receiver: receive.Receiver) -> None:
    receiver.prepare()
    release = receive.Release(
        {"release/config/nested/web": b"config", "release/compose.yaml": b"compose"},
        {"token": b"secret"},
        "abc123",
    )
    with patch.object(fs, "mkdir", wraps=fs.mkdir) as mkdir:
        receiver.stage(release)
    assert receiver.incoming is not None
    assert mkdir.call_args_list[0] == call(receiver.incoming / "release", 0o755)
    directory = receiver.service.root / "releases" / RELEASE
    assert receiver.created is True
    assert (directory / "config" / "nested" / "web").read_bytes() == b"config"
    assert (directory / "compose.yaml").read_bytes() == b"compose"
    assert (directory.stat().st_mode & 0o777) == 0o755
    assert ((directory / ".sha256").stat().st_mode & 0o777) == 0o644
    assert ((directory / "config").stat().st_mode & 0o777) == 0o755
    assert ((directory / "config" / "nested" / "web").stat().st_mode & 0o777) == 0o644
    assert (directory / ".sha256").read_bytes() == b"abc123"
    assert receiver.incoming is not None
    assert receiver.incoming.parent == receiver.service.root / "incoming"
    assert not (receiver.incoming / "release").exists()
    assert not receiver.service.secrets[0].target.exists()
    receiver.created = False
    receiver.stage(release)
    assert vars(receiver)["created"] is False
    with pytest.raises(RefusalError, match=r"^release digest conflict$"):
        receiver.stage(replace(release, digest="different"))


@pytest.mark.parametrize("manual", [False, True])
def test_receive_entry_exact_construction(
    deploy_service: Service, monkeypatch: pytest.MonkeyPatch, manual: bool
) -> None:
    monkeypatch.setenv("SUDO_USER", "operator" if manual else "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE + " 456")
    service_doc: Json = {"service": "o11y"}
    config_doc: Json = {"compose_binary": "/compose", "compose_version": "5.7.1"}
    output = io.StringIO()
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(receive.time, "monotonic", return_value=123),
        patch.object(receive, "load", side_effect=[service_doc, config_doc]) as load,
        patch.object(receive, "service", return_value=deploy_service) as service,
        patch.object(receive, "Command") as command,
        patch.object(receive, "verify_binary") as verify,
        patch.object(receive, "Engine") as engine_type,
        patch.object(receive, "Receiver") as receiver_type,
    ):
        command.return_value.return_value = b" 5.7.1\n"
        receiver_type.return_value.run.return_value = 7
        assert receive.receive(output, rollback="o11y" if manual else None) == 7
    assert load.call_args_list == [
        call(receive.CONFIG / "services" / "o11y.toml"),
        call(receive.CONFIG / "receiver.toml"),
    ]
    service.assert_called_once_with(service_doc, "o11y")
    verify.assert_called_once_with(Path("/compose"))
    command.return_value.assert_called_once_with(
        ["/compose", "version", "--short"],
        timeout_s=10,
        stdout_limit=4096,
        stderr_limit=4096,
    )
    engine_type.assert_called_once_with(
        deploy_service, "/compose", command.return_value
    )
    receiver_type.assert_called_once_with(
        Request("o11y", "0" * 40 if manual else RELEASE, None if manual else "456"),
        deploy_service,
        engine_type.return_value,
        output,
        123,
    )
    receiver_type.return_value.run.assert_called_once_with(manual=manual)
    assert output.getvalue() == ""


@pytest.mark.parametrize("limit", [3, 100000])
def test_stream_selector_and_read_bounds(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    monkeypatch.setattr(receive.sys, "stdin", Mock(fileno=Mock(return_value=31)))
    with (
        patch.object(receive.selectors, "DefaultSelector") as selector_type,
        patch.object(receive.time, "monotonic", side_effect=[10, 11, 12]),
        patch.object(receive.os, "read", side_effect=[b"abc", b""]) as read,
    ):
        selector = selector_type.return_value.__enter__.return_value
        selector.select.return_value = [(31, receive.selectors.EVENT_READ)]
        assert receive.stream(limit) == b"abc"
    selector.register.assert_called_once_with(31, receive.selectors.EVENT_READ)
    assert selector.select.call_args_list == [call(299), call(298)]
    assert read.call_args_list == [
        call(31, min(65536, limit + 1)),
        call(31, min(65536, limit - 2)),
    ]


@pytest.mark.parametrize("remaining", [0, -1, 1])
def test_stream_deadline_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, remaining: int
) -> None:
    monkeypatch.setattr(receive.sys, "stdin", Mock(fileno=Mock(return_value=31)))
    with (
        patch.object(receive.selectors, "DefaultSelector") as selector_type,
        patch.object(receive.time, "monotonic", side_effect=[0, 300 - remaining]),
        patch.object(receive.os, "read") as read,
    ):
        selector = selector_type.return_value.__enter__.return_value
        selector.select.return_value = ()
        with pytest.raises(RefusalError, match=r"^stream deadline$"):
            receive.stream(3)
    read.assert_not_called()
    if remaining > 0:
        selector.select.assert_called_once_with(remaining)
    else:
        selector.select.assert_not_called()


def test_run_exact_progress_and_engine_calls(
    receiver: receive.Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver.started = 10
    monkeypatch.setattr(receive.time, "monotonic", lambda: 11.23)
    with (
        patch.object(receive, "stream", wraps=receive.stream) as stream,
        patch.object(receiver.engine, "run", wraps=receiver.engine.run) as run,
        patch.object(
            receiver.engine, "check_paths", wraps=receiver.engine.check_paths
        ) as paths,
        patch.object(receiver.engine, "health", wraps=receiver.engine.health) as health,
    ):
        assert receiver.run() == 0
    stream.assert_called_once_with(64 * 1024**2)
    assert run.call_args_list == [
        call(RELEASE, "pull"),
        call(RELEASE, "up", "-d", "--wait", "--remove-orphans", "--force-recreate"),
    ]
    paths.assert_called_once_with({}, RELEASE)
    health.assert_called_once_with(RELEASE)
    assert isinstance(receiver.out, io.StringIO)
    records = [json.loads(line) for line in receiver.out.getvalue().splitlines()]
    assert records == [
        {
            "stack": "o11y",
            "release": RELEASE,
            "run_id": "123",
            "duration_s": 1.2,
            "step": step,
            "outcome": outcome,
        }
        for step, outcome in [
            ("lock", "progress"),
            ("space", "progress"),
            ("release", "progress"),
            ("policy", "progress"),
            ("secrets", "progress"),
            ("pull", "progress"),
            ("switch", "progress"),
            ("health", "progress"),
            ("done", "progress"),
            ("done", "ok"),
        ]
    ]
    assert (
        receiver.service.root / "releases" / RELEASE / "compose.yaml"
    ).read_bytes() == b"model"
    assert (
        receiver.service.root / "releases" / RELEASE / "config" / "web"
    ).read_bytes() == b"config"
    assert fs.target(receiver.service.root / "previous") is None
    assert receiver.signals.original
    assert not (receiver.service.root / "deploy" / "force-recreate").exists()
    assert not list((receiver.service.root / "incoming").iterdir())


@pytest.mark.parametrize("free", [1024**3 - 1, 1024**3, 1024**3 + 1])
def test_free_space_boundary(receiver: receive.Receiver, free: int) -> None:
    receiver.service = replace(receiver.service, min_free_gib=1)
    usage = Mock(free=free)
    with patch.object(receive.shutil, "disk_usage", return_value=usage) as disk:
        assert receiver.run() == (2 if free < 1024**3 else 0)
    disk.assert_called_once_with(receiver.service.root)
    assert result(receiver)["step"] == ("space" if free < 1024**3 else "done")
    if free < 1024**3:
        assert result(receiver)["reason"] == "free space"


@pytest.mark.parametrize(
    "info", [None, os.stat_result((0o100700, 0, 0, 0, 1, 0, 0, 0, 0, 0))]
)
def test_prepare_init_owner(
    receiver: receive.Receiver, info: os.stat_result | None
) -> None:
    receiver.service = replace(receiver.service, init=("/root/init", "argument"))
    root_info = os.stat_result((0o040700, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    with (
        patch.object(fs, "inspect", side_effect=[root_info, info]) as inspect,
        patch.object(fs, "mkdir") as mkdir,
        pytest.raises(RefusalError, match=r"^init executable owner$"),
    ):
        receiver.prepare()
    assert inspect.call_args_list == [
        call(receiver.service.root),
        call(Path("/root/init"), file=True),
    ]
    assert mkdir.call_args_list == [
        call(receiver.service.root / name)
        for name in ("deploy", "incoming", "releases")
    ] + [call(receiver.service.root / "deploy" / "models")]


@pytest.mark.parametrize("known", [False, True])
def test_entry_refusal_record_exact(
    monkeypatch: pytest.MonkeyPatch, known: bool
) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE + " 456")
    output = io.StringIO()
    with (
        patch.object(receive.time, "monotonic", side_effect=[100, 102.36]),
        patch.object(os, "geteuid", return_value=0 if known else 1),
        patch.object(receive, "load", side_effect=RefusalError("safe refusal")),
    ):
        assert receive.receive(output) == 2
    assert (
        output.getvalue()
        == json.dumps(
            {
                "stack": "o11y" if known else None,
                "release": RELEASE if known else None,
                "outcome": "refused",
                "step": "request",
                "duration_s": 2.4,
                "run_id": "456" if known else None,
                "reason": "safe refusal" if known else "receive requires root",
            }
        )
        + "\n"
    )


def test_entry_unix_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RefusalError, match=r"^receive requires Unix$"):
        receive.receive(io.StringIO())


@pytest.mark.parametrize("failure", [CommandError, OSError])
def test_journal_command_failure_does_not_block_result_output(
    receiver: receive.Receiver, failure: type[Exception]
) -> None:
    with patch.object(receiver.engine, "command", side_effect=failure):
        receiver.line("ok")
    assert result(receiver)["outcome"] == "ok"


def test_cleanup_removes_candidate_model_and_release_digest(
    receiver: receive.Receiver,
) -> None:
    receiver.prepare()
    receiver.incoming = receiver.service.root / "incoming" / "staging"
    receiver.incoming.mkdir()
    directory = receiver.service.root / "releases" / RELEASE
    directory.mkdir()
    model = receiver.engine.model_path(RELEASE)
    digest = directory / ".sha256"
    fs.write(model, b"{}")
    fs.write(digest, b"digest")
    receiver.created = True
    receiver.cleanup(before_secrets=True)
    assert not receiver.incoming.exists()
    assert not directory.exists()
    assert not model.exists()
    assert not digest.exists()


def test_rollback_refuses_a_missing_transaction(receiver: receive.Receiver) -> None:
    transaction = Transaction(receiver.service, receiver.engine)
    with (
        patch.object(transaction, "journal", return_value=None),
        pytest.raises(RefusalError, match=r"^missing transaction$"),
    ):
        receiver.rollback(transaction)


def test_rollback_recreates_the_recorded_release(receiver: receive.Receiver) -> None:
    seed_previous(receiver)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save(RELEASE)
    with (
        patch.object(receiver, "progress", wraps=receiver.progress) as progress,
        patch.object(receiver.engine, "up", wraps=receiver.engine.up) as up,
    ):
        assert receiver.rollback(transaction) == (1, "rolled_back")
    progress.assert_called_once_with("rollback")
    up.assert_called_once_with(PREVIOUS, recreate=True)


def test_run_passes_exact_release_and_stream_limit_to_each_stage(
    receiver: receive.Receiver,
) -> None:
    limit = receiver.service.max_stream_mib * 1024**2
    body = receive.stream(limit)
    transaction = Transaction(receiver.service, receiver.engine)
    with (
        patch.object(receive, "unpack", wraps=receive.unpack) as unpack,
        patch.object(receive, "Transaction", return_value=transaction),
        patch.object(transaction, "save", wraps=transaction.save) as save,
        patch.object(receiver.engine, "parse", wraps=receiver.engine.parse) as parse,
        patch.object(receiver.engine, "stored", wraps=receiver.engine.stored) as stored,
    ):
        assert receiver.run() == 0
    unpack.assert_called_once_with(body, receiver.wanted, {"token"}, limit)
    save.assert_called_once_with(RELEASE)
    parse.assert_called_once_with(RELEASE)
    stored.assert_called_once_with(RELEASE)


def test_failure_after_secret_phase_is_failed_without_a_journal(
    receiver: receive.Receiver,
) -> None:
    with patch.object(receiver, "apply", side_effect=CommandError):
        assert receiver.run() == 3
    assert result(receiver)["outcome"] == "failed"
    assert result(receiver)["reason"] == "receiver step failed"


def test_failure_before_secret_phase_is_a_refusal(receiver: receive.Receiver) -> None:
    with patch.object(receiver.engine, "parse", side_effect=CommandError):
        assert receiver.run() == 2
    assert result(receiver)["outcome"] == "refused"
    assert result(receiver)["reason"] == "receiver step failed"


def test_missing_original_command_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    output = io.StringIO()
    with patch.object(os, "geteuid", return_value=0):
        assert receive.receive(output) == 2
    assert json.loads(output.getvalue())["stack"] is None
    assert json.loads(output.getvalue())["reason"] == "invalid request"


def test_entry_rejects_unknown_receiver_configuration(
    deploy_service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE)
    config_doc: Json = {
        "compose_binary": "/compose",
        "compose_version": "5.7.1",
        "unknown": True,
    }
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(receive, "load", side_effect=[{}, config_doc]),
        patch.object(receive, "service", return_value=deploy_service),
        patch.object(receive, "Command") as command,
    ):
        output = io.StringIO()
        assert receive.receive(output) == 2
    command.assert_not_called()
    assert json.loads(output.getvalue())["reason"] == "unknown object key"


@pytest.mark.parametrize(
    ("binary", "version"), [("relative-compose", "5.7.1"), ("/compose", "5.7.0")]
)
def test_entry_rejects_binary_and_configured_version_before_command(
    deploy_service: Service,
    monkeypatch: pytest.MonkeyPatch,
    binary: str,
    version: str,
) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE)
    configuration: Json = {"compose_binary": binary, "compose_version": version}
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(receive, "load", Mock(side_effect=[{}, configuration]))
    monkeypatch.setattr(receive, "service", Mock(return_value=deploy_service))
    output = io.StringIO()
    with patch.object(receive, "Command") as command:
        assert receive.receive(output) == 2
    command.assert_not_called()
    assert json.loads(output.getvalue())["reason"] == (
        "Compose binary path"
        if binary == "relative-compose"
        else "unsupported Compose version"
    )


@pytest.mark.parametrize("after_rename", [False, True])
def test_digest_precedes_release_publish(
    receiver: receive.Receiver, after_rename: bool
) -> None:
    receiver.prepare()
    release = receive.Release({"release/config": b"config"}, {}, "digest")
    destination = receiver.service.root / "releases" / RELEASE
    original = Path.rename

    def interrupted(source: Path, target: Path) -> Path:
        assert target == destination
        assert (source / ".sha256").read_bytes() == b"digest"
        if after_rename:
            original(source, target)
        message = "injected publish crash"
        raise OSError(message)

    with (
        patch.object(Path, "rename", interrupted),
        pytest.raises(OSError, match="publish crash"),
    ):
        receiver.stage(release)
    assert destination.exists() is after_rename
    if after_rename:
        assert (destination / ".sha256").read_bytes() == b"digest"
    receiver.stage(release)
    assert (destination / ".sha256").read_bytes() == b"digest"


def test_legacy_published_release_without_digest_refuses(
    receiver: receive.Receiver,
) -> None:
    receiver.prepare()
    destination = receiver.service.root / "releases" / RELEASE
    destination.mkdir()
    release = receive.Release({}, {}, "digest")
    for _ in range(2):
        with pytest.raises(RefusalError, match=r"^published release missing digest$"):
            receiver.stage(release)
    assert destination.is_dir()
    assert not (destination / ".sha256").exists()


def test_manual_recovery_restart_failure_retains_journal_and_fails(
    receiver: receive.Receiver,
) -> None:
    receiver.prepare()
    fs.pointer(receiver.service.root / "current", "releases/" + PREVIOUS)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save(RELEASE)
    engine(receiver).fail_at.add("up " + PREVIOUS)
    assert receiver.run(manual=True) == 3
    assert result(receiver)["outcome"] == "failed"
    assert result(receiver)["reason"] == "recovery failed"
    assert transaction.journal() is not None


@pytest.mark.parametrize("failure", [KeyError, TypeError])
def test_manual_apply_schema_failure_reports_failed_recovery(
    receiver: receive.Receiver, failure: type[Exception]
) -> None:
    receiver.prepare()
    fs.pointer(receiver.service.root / "previous", "releases/" + PREVIOUS)
    with patch.object(receiver, "apply", side_effect=failure):
        assert receiver.run(manual=True) == 3
    assert result(receiver)["outcome"] == "failed"
    assert result(receiver)["reason"] == "recovery failed"


def test_bad_journal_name_reports_without_reading_it_again(
    receiver: receive.Receiver,
) -> None:
    receiver.prepare()
    transaction = Transaction(receiver.service, receiver.engine)
    journal = transaction.save(RELEASE)
    journal["current"] = "../outside"
    transaction.update(journal, "open")
    assert receiver.run() == 3
    assert result(receiver)["reason"] == "pointer target"


@pytest.mark.parametrize("failure", [KeyError, TypeError])
def test_manual_entry_run_schema_failure_reports_failed_recovery(
    deploy_service: Service, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    monkeypatch.setenv("SUDO_USER", "operator")
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(
            receive,
            "load",
            side_effect=[
                {},
                {"compose_binary": "/compose", "compose_version": "5.7.1"},
            ],
        ),
        patch.object(receive, "service", return_value=deploy_service),
        patch.object(receive, "verify_binary"),
        patch.object(receive, "Command", return_value=Mock(return_value=b"5.7.1")),
        patch.object(receive.Receiver, "run", side_effect=failure),
    ):
        output = io.StringIO()
        assert receive.receive(output, rollback="o11y") == 3
    record = json.loads(output.getvalue())
    assert record["outcome"] == "failed"
    assert record["step"] == "recovery"
    assert record["reason"] == "recovery failed"


@pytest.mark.parametrize(
    ("uid", "sudo_user", "rollback", "reason"),
    [
        (1, "deploy", None, "receive requires root"),
        (0, "operator", None, "receive requires deploy sudo user"),
        (0, "deploy", "o11y", "deploy cannot request rollback"),
        (0, "operator", "bad name", "rollback service"),
    ],
)
def test_identity_refusal_reasons(
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    sudo_user: str,
    rollback: str | None,
    reason: str,
) -> None:
    monkeypatch.setenv("SUDO_USER", sudo_user)
    monkeypatch.setattr(os, "geteuid", lambda: uid)
    with pytest.raises(RefusalError, match="^" + reason + "$"):
        receive.identity(rollback)


def test_manual_contents_refuses_missing_previous_with_a_distinct_reason(
    receiver: receive.Receiver,
) -> None:
    receiver.prepare()
    with pytest.raises(RefusalError, match=r"^no previous release$"):
        receiver.contents(None)


def test_existing_digest_requires_regular_control_file(
    receiver: receive.Receiver,
) -> None:
    receiver.prepare()
    destination = receiver.service.root / "releases" / RELEASE
    destination.mkdir()
    (destination / ".sha256").mkdir()
    with pytest.raises(RefusalError, match=r"^path type$"):
        receiver.stage(receive.Release({}, {}, "digest"))


def test_rollback_without_prior_release_returns_failed_status(
    receiver: receive.Receiver,
) -> None:
    transaction = Mock()
    journal: dict[str, Json] = {"current": None}
    transaction.journal.return_value = journal
    assert receiver.rollback(transaction) == (3, "failed")
    transaction.delete.assert_called_once_with()


@pytest.mark.parametrize(
    "failure", [OSError, ValueError, CommandError, KeyError, TypeError]
)
def test_entry_redacts_non_protocol_errors(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE)
    output = io.StringIO()
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(receive, "load", side_effect=failure("private output")),
    ):
        assert receive.receive(output) == 2
    assert json.loads(output.getvalue())["reason"] == "request refused"
    assert "private output" not in output.getvalue()


@pytest.mark.parametrize(
    ("wanted", "fields"),
    [
        (None, (None, None, None)),
        (Request("o11y", RELEASE, "123"), ("o11y", RELEASE, "123")),
        (Request("o11y", RELEASE, None), ("o11y", RELEASE, None)),
    ],
)
def test_refusal_identity_preserves_only_known_protocol_fields(
    wanted: Request | None, fields: tuple[str | None, str | None, str | None]
) -> None:
    assert receive.identity_fields(wanted) == fields


@pytest.mark.parametrize("writable", [False, True])
def test_prepare_walks_init_ancestors(
    receiver: receive.Receiver, writable: bool
) -> None:
    directory = receiver.service.root / "trusted-bin"
    directory.mkdir()
    executable = directory / "init"
    executable.write_bytes(b"init")
    receiver.service = replace(receiver.service, init=(str(executable),))
    original = os.fstat

    def directory_mode(fd: int) -> os.stat_result:
        actual = original(fd)
        fields = list(actual)
        if writable and actual.st_ino == directory.stat().st_ino:
            fields[0] = actual.st_mode | 0o020
        return os.stat_result(fields)

    with patch.object(os, "fstat", directory_mode):
        if writable:
            with pytest.raises(RefusalError, match=r"^writable directory$"):
                receiver.prepare()
        else:
            receiver.prepare()


@pytest.mark.parametrize("failure", ["pull", "init"])
def test_repeated_padded_failures_prune_before_space_and_remove_release(
    receiver: receive.Receiver, failure: str
) -> None:
    receiver.prepare()
    root = receiver.service.root
    releases = root / "releases"
    for number in range(8):
        abandoned = releases / f"{number:040x}"
        abandoned.mkdir()
        (abandoned / "padding").write_bytes(b"x" * 4096)
    receiver.service = replace(receiver.service, min_free_gib=1)
    if failure == "init":
        receiver.service = replace(receiver.service, init=("/root/init",))

    def space(_path: Path) -> object:
        # Simulate a filesystem whose free space is exhausted by abandoned releases.
        free = 1024**3 if len(list(releases.iterdir())) <= 5 else 0
        return Mock(free=free)

    with (
        patch.object(receive.shutil, "disk_usage", side_effect=space),
        patch.object(receiver, "prepare"),
    ):
        for number in range(10, 18):
            release = f"{number:040x}"
            receiver.wanted = Request("o11y", release, None)
            receiver.created = False
            body = pack(
                receiver.wanted, {"release/padding": b"x" * 4096}, {"token": b"fixture"}
            )
            engine(receiver).fail_at = {"pull " + release}
            fake = receiver.engine.command
            assert isinstance(fake, FakeCommand)
            fake.fail_argv = list(receiver.service.init) if failure == "init" else []
            with patch.object(receive, "stream", return_value=body):
                assert receiver.run() == 4
            assert result(receiver)["outcome"] == "aborted"
            assert len(list(releases.iterdir())) <= 5
            assert not (releases / release).exists()
            assert not receiver.engine.model_path(release).exists()


def test_startup_stops_units_after_preflight_before_release_writes(
    receiver: receive.Receiver,
) -> None:
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    fake: FakeCommand = command
    fake.active = True

    def prepare_after_stop(release: receive.Release) -> None:
        argv = [command for command, _data in fake.calls]
        assert ["/usr/bin/systemctl", "stop", "tbhb-deploy-o11y"] in argv
        assert ["/usr/bin/systemctl", "stop", "tbhb-deploy-parse-o11y"] in argv
        assert argv.index(["/usr/bin/docker", "info", "--format", "{{json .}}"]) < next(
            i
            for i, command in enumerate(argv)
            if command[:2] == ["/usr/bin/systemctl", "stop"]
        )
        assert release.files["release/compose.yaml"] == b"model"
        message = "stage checked"
        raise RefusalError(message)

    with (
        patch.object(
            receiver.engine, "stop_unit", Engine.stop_unit.__get__(receiver.engine)
        ),
        patch.object(receiver, "stage", side_effect=prepare_after_stop),
    ):
        assert receiver.run() == 2
    assert result(receiver)["step"] == "release"
    assert result(receiver)["reason"] == "stage checked"


def test_chained_pax_refusal_reports_stream_step(receiver: receive.Receiver) -> None:
    with patch.object(receive, "stream", return_value=chained_pax()):
        assert receiver.run() == 2
    assert result(receiver)["outcome"] == "refused"
    assert result(receiver)["step"] == "stream"
    assert result(receiver)["reason"] == "invalid tar stream"


@pytest.mark.parametrize("failure", ["pull", "up", "health"])
def test_compensation_waits_for_units_or_retains_journal(
    receiver: receive.Receiver, failure: str
) -> None:
    seed_previous(receiver)
    daemon = engine(receiver)
    daemon.fail_at = {failure + " " + RELEASE}
    original = daemon.stop_unit

    def unconfirmed() -> None:
        original()
        if daemon.events.count("stop-unit") > 1:
            message = "transient unit is not inactive; compensation withheld"
            raise RefusalError(message)

    with patch.object(daemon, "stop_unit", side_effect=unconfirmed):
        assert receiver.run() == 3
    assert result(receiver)["outcome"] == "failed"
    assert (
        result(receiver)["reason"]
        == "transient unit is not inactive; compensation withheld"
    )
    assert daemon.events.count("stop-unit") == 2
    assert "stop" not in daemon.events
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"
    assert fs.target(receiver.service.root / "current") == "releases/" + (
        PREVIOUS if failure == "pull" else RELEASE
    )
    transaction = Transaction(receiver.service, daemon)
    journal = transaction.journal()
    assert journal is not None
    assert journal["state"] == "open"
    assert (transaction.txn / "saved" / "payload" / "token").read_bytes() == b"earlier"
    assert (receiver.service.root / "releases" / RELEASE).exists()


@pytest.mark.parametrize("failure", ["pull", "up", "health"])
def test_compensation_confirms_units_before_secret_restore(
    receiver: receive.Receiver, failure: str
) -> None:
    seed_previous(receiver)
    daemon = engine(receiver)
    daemon.fail_at = {failure + " " + RELEASE}
    restore = Transaction.restore

    def checked_restore(transaction: Transaction, journal: dict[str, Json]) -> None:
        assert daemon.events.count("stop-unit") == 2
        if failure != "pull":
            assert daemon.events[-2:] == ["stop-unit", "stop"]
        restore(transaction, journal)

    with patch.object(Transaction, "restore", checked_restore):
        assert receiver.run() == (4 if failure == "pull" else 1)


def test_identical_retry_preserves_previous_release_and_secrets(
    receiver: receive.Receiver,
) -> None:
    align_secret_group(receiver)
    wanted = receiver.wanted
    receiver.wanted = Request(wanted.service, PREVIOUS, None)
    body = pack(
        receiver.wanted,
        {"release/compose.yaml": b"previous model"},
        {"token": b"earlier"},
    )
    with patch.object(receive, "stream", return_value=body):
        assert receiver.run() == 0
    receiver.wanted = wanted
    receiver.created = False
    assert receiver.run() == 0
    transaction = Transaction(receiver.service, receiver.engine)
    saved = transaction.state / "last-secrets"
    snapshot = {
        path.relative_to(saved): path.read_bytes()
        for path in saved.rglob("*")
        if path.is_file()
    }
    receiver.created = False
    assert receiver.run() == 0
    record = result(receiver)
    assert record["outcome"] == "ok"
    assert record["step"] == "done"
    assert engine(receiver).events.count("health " + RELEASE) == 2
    assert engine(receiver).events.count("up " + RELEASE) == 2
    assert engine(receiver).events.count("force-recreate") == 2
    assert fs.target(receiver.service.root / "previous") == "releases/" + PREVIOUS
    assert {
        path.relative_to(saved): path.read_bytes()
        for path in saved.rglob("*")
        if path.is_file()
    } == snapshot
    assert receiver.run(manual=True) == 0
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"


class RetryCommand(FakeCommand):
    """Model the daemon state changed by Compose and read by real health checks."""

    def __init__(self) -> None:
        super().__init__()
        self.running = {"web": False, "worker": False}
        self.fail_up = False
        self.fail_health = False
        self.model = {
            "name": "o11y",
            "services": {
                name: mapping(minimal()["services"])["web"] for name in self.running
            },
        }

    @override
    def __call__(
        self, argv: list[str], data: bytes | None = None, **kwargs: object
    ) -> bytes:
        self.inspected = [
            {
                "Config": {"Labels": {"com.docker.compose.service": name}},
                "State": {"Running": running},
                "Mounts": [],
            }
            for name, running in self.running.items()
        ]
        output = super().__call__(argv, data, **kwargs)
        if "up" in argv:
            if self.fail_up:
                raise CommandError
            self.running = dict.fromkeys(self.running, not self.fail_health)
        return output


@pytest.fixture
def retry_receiver(
    receiver: receive.Receiver, monkeypatch: pytest.MonkeyPatch
) -> receive.Receiver:
    secret = receiver.service.secrets[0]
    receiver.service = replace(
        receiver.service,
        secrets=(replace(secret, gid=secret.target.parent.stat().st_gid),),
        timeout_s=1,
    )
    receiver.engine = Engine(receiver.service, "/compose", RetryCommand())
    use_health_clock(receiver.engine, monkeypatch)
    seed_previous(receiver)
    assert receiver.run() == 0
    receiver.created = False
    return receiver


def use_health_clock(daemon: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance simulated daemon failures without permitting retries past the budget."""
    original = daemon.health

    def health(release: str) -> None:
        now = 0.0

        def advance(seconds: float) -> None:
            nonlocal now
            assert now < daemon.service.timeout_s
            assert 0 < seconds <= daemon.service.timeout_s - now
            now += seconds

        with (
            patch.object(compose.time, "monotonic", side_effect=lambda: now),
            patch.object(compose.time, "sleep", side_effect=advance),
        ):
            original(release)

    monkeypatch.setattr(daemon, "health", health)


def retry_restarts(receiver: receive.Receiver, stopped: str) -> None:
    command = receiver.engine.command
    assert isinstance(command, RetryCommand)
    assert receiver.service.health_services == ("web",)
    assert command.running == {"web": True, "worker": True}
    command.running[stopped] = False
    command.calls.clear()
    assert receiver.run() == 0
    assert result(receiver)["outcome"] == "ok"
    assert command.running == {"web": True, "worker": True}
    up = next(argv for argv, _data in command.calls if "up" in argv)
    assert up[-4:] == ["up", "-d", "--wait", "--remove-orphans"]
    assert all("pull" not in argv for argv, _data in command.calls)
    inspected = [
        i for i, (argv, _data) in enumerate(command.calls) if "inspect" in argv
    ]
    assert inspected[-1] > command.calls.index((up, None))
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE
    assert fs.target(receiver.service.root / "previous") == "releases/" + PREVIOUS


def test_identical_retry_restarts_stopped_unmonitored_service(
    retry_receiver: receive.Receiver,
) -> None:
    retry_restarts(retry_receiver, "worker")


def test_identical_retry_restarts_stopped_health_gated_service(
    retry_receiver: receive.Receiver,
) -> None:
    retry_restarts(retry_receiver, "web")


@pytest.mark.parametrize("failure", ["up", "health"])
def test_identical_retry_failure_preserves_current_and_rollback_state(
    retry_receiver: receive.Receiver, failure: str
) -> None:
    receiver = retry_receiver
    command = receiver.engine.command
    assert isinstance(command, RetryCommand)
    command.fail_up = failure == "up"
    command.fail_health = failure == "health"
    command.running["web"] = False
    saved = receiver.service.root / "deploy" / "last-secrets"
    snapshot = {path: path.read_bytes() for path in saved.rglob("*") if path.is_file()}
    secret = receiver.service.secrets[0].target
    before = secret.stat()
    command.calls.clear()
    assert receiver.run() == 3
    assert result(receiver)["outcome"] == "failed"
    assert result(receiver)["step"] == "rollback"
    assert result(receiver)["reason"] == "receiver step failed"
    assert any("up" in argv for argv, _data in command.calls)
    assert any("stop" in argv for argv, _data in command.calls)
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE
    assert fs.target(receiver.service.root / "previous") == "releases/" + PREVIOUS
    assert {path: path.read_bytes() for path in snapshot} == snapshot
    assert secret.read_bytes() == b"fixture"
    assert secret.stat().st_mode == before.st_mode
    assert Transaction(receiver.service, receiver.engine).journal() is not None
    assert (receiver.service.root / "releases" / RELEASE).is_dir()


def test_failed_auto_recovery_retains_journal_until_verified_restart(
    receiver: receive.Receiver,
) -> None:
    seed_previous(receiver)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save(RELEASE)
    transaction.write({"token": b"interrupted"})
    fs.pointer(receiver.service.root / "current", "releases/" + RELEASE)
    daemon = engine(receiver)
    daemon.fail_at = {"health " + PREVIOUS}
    with patch.object(receive, "stream", return_value=b"invalid"):
        assert receiver.run() == 2
    assert result(receiver)["reason"] == "invalid tar stream"
    assert transaction.journal() is not None
    assert daemon.events == []
    assert receiver.run(manual=True) == 3
    assert result(receiver)["reason"] == "recovery failed"
    assert transaction.journal() is not None
    assert isinstance(receiver.out, io.StringIO)
    assert '"outcome": "recovered"' not in receiver.out.getvalue()
    daemon.fail_at.clear()
    assert receiver.run(manual=True) == 0
    assert transaction.journal() is None
    assert result(receiver)["outcome"] == "ok"
    assert daemon.events.count("up " + PREVIOUS) == 2
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"


@pytest.mark.parametrize("failure", [ValueError, KeyError, TypeError, RuntimeError])
@pytest.mark.parametrize("step", ["pull", "up", "health"])
def test_unexpected_apply_failure_compensates(
    receiver: receive.Receiver, failure: type[Exception], step: str
) -> None:
    seed_previous(receiver)
    daemon = engine(receiver)
    original = daemon.event

    def event(name: str) -> None:
        original(name)
        if name == step + " " + RELEASE:
            raise failure

    with patch.object(daemon, "event", side_effect=event):
        assert receiver.run() == (4 if step == "pull" else 1)
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    assert not (receiver.service.root / "deploy" / "txn").exists()


def test_crash_after_health_before_commit_recovers_previous(
    receiver: receive.Receiver,
) -> None:
    seed_previous(receiver)
    with (
        patch.object(Transaction, "finish", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        receiver.run()
    assert "health " + RELEASE in engine(receiver).events
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE
    assert Transaction(receiver.service, receiver.engine).journal() is not None
    assert receiver.run(manual=True) == 0
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"


def test_new_release_with_unchanged_secrets_uses_normal_compose_up(
    receiver: receive.Receiver,
) -> None:
    seed_previous(receiver)
    secret = receiver.service.secrets[0]
    fs.write(secret.target, b"fixture", secret.mode)
    receiver.service = replace(
        receiver.service, secrets=(replace(secret, gid=secret.target.stat().st_gid),)
    )
    receiver.engine.service = receiver.service
    with patch.object(receiver.engine, "run", wraps=receiver.engine.run) as run:
        assert receiver.run() == 0
    assert run.call_args_list == [
        call(RELEASE, "pull"),
        call(RELEASE, "up", "-d", "--wait", "--remove-orphans"),
    ]
    assert fs.target(receiver.service.root / "previous") == "releases/" + PREVIOUS


def test_apply_withholds_compensation_after_health_stop_refusal(
    receiver: receive.Receiver,
) -> None:
    receiver.prepare()
    transaction = Transaction(receiver.service, receiver.engine)
    with (
        patch.object(
            receiver.engine,
            "health",
            side_effect=RefusalError("unit stop unconfirmed; compensation withheld"),
        ),
        patch.object(receiver, "rollback") as rollback,
        pytest.raises(RefusalError, match="compensation withheld"),
    ):
        receiver.apply(transaction, {"token": b"candidate"}, manual=False)
    rollback.assert_not_called()
    journal = transaction.journal()
    assert journal is not None
    assert journal["state"] == "open"
    assert receiver.service.secrets[0].target.read_bytes() == b"candidate"


@pytest.mark.parametrize("local", [False, True])
def test_manual_rollback_pulls_only_missing_images(
    receiver: receive.Receiver, local: bool
) -> None:
    seed_previous(receiver)
    assert receiver.run() == 0
    receiver.created = False
    engine(receiver).events.clear()
    with patch.object(receiver.engine, "local_images", return_value=local) as images:
        assert receiver.run(manual=True) == 0
    images.assert_called_once_with(PREVIOUS)
    assert ("pull " + PREVIOUS in engine(receiver).events) is not local
    assert "up " + PREVIOUS in engine(receiver).events
