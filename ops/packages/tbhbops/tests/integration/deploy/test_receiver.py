"""Verify receiver orchestration, preflight, and interrupted recovery."""

import io
import json
import os
import signal
import sys
import tarfile
from dataclasses import replace
from typing import override
from unittest.mock import Mock, call, patch

import pytest
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy import receiver as receiver_module
from tbhbops.deploy import receiver_io, staging
from tbhbops.deploy.activation import Activation
from tbhbops.deploy.command import CommandError
from tbhbops.deploy.compose import Engine
from tbhbops.deploy.protocol import (
    Json,
    RefusalError,
    Release,
    Request,
    pack,
    sha256,
)
from tbhbops.deploy.receiver import Receiver
from tbhbops.deploy.transaction import Lock, Transaction
lazy from tbhbops.deploy.config import Service

from ...deploy_fakes import FakeCommand
from ...deploy_model import RELEASE, minimal
from ...deploy_receiver import (
    PREVIOUS,
    ReceiveEngine,
    engine,
    result,
    seed_previous,
)
from ...test_deploy_protocol import chained_pax


@pytest.fixture
def capability_receiver(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> Receiver:
    # The isolated Compose parser supplies a valid model; the daemon read and
    # every receiver operation use the real preflight and transaction paths.
    monkeypatch.setattr(receiver.engine, "sandbox", Mock(return_value=minimal()))
    return receiver


def refused_capability(receiver: Receiver, reason: str) -> None:
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
    capability_receiver: Receiver,
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
    capability_receiver: Receiver,
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
    capability_receiver: Receiver,
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
    receiver: Receiver, failure: type[Exception]
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
    capability_receiver: Receiver,
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
    capability_receiver: Receiver, required: bool
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
    capability_receiver: Receiver, key: str, value: Json
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
    capability_receiver: Receiver, key: str, value: Json
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    info = json.loads(command.info)
    info[key] = value
    command.info = json.dumps(info).encode()
    refused_capability(capability_receiver, "daemon capability report invalid: " + key)


@pytest.mark.parametrize("key", ["PidsLimit", "MemoryLimit", "SwapLimit"])
def test_refuses_missing_daemon_capability(
    capability_receiver: Receiver, key: str
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
    capability_receiver: Receiver, body: bytes
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
    capability_receiver: Receiver, failure: Exception
) -> None:
    command = capability_receiver.engine.command
    assert isinstance(command, FakeCommand)
    command.info_error = failure
    refused_capability(capability_receiver, "daemon capability report unavailable")


@pytest.mark.parametrize("extra", [0, 1])
def test_payload_bytes_boundary(receiver: Receiver, extra: int) -> None:
    receiver.service = replace(receiver.service, max_stream_mib=1)
    files = {"release/compose.yaml": b""}
    secrets = {"token": b"fixture"}
    overhead = len(pack(receiver.wanted, files, secrets))
    files["release/compose.yaml"] = bytes(1024**2 - overhead)
    body = pack(receiver.wanted, files, secrets)
    assert len(body) == 1024**2
    with patch.object(
        receiver_io, "stream", return_value=body + bytes(extra)
    ) as stream:
        assert receiver.run() == (2 if extra else 0)
    stream.assert_called_once_with(1024**2)
    if extra:
        assert result(receiver)["reason"] == "stream limit"
        assert result(receiver)["outcome"] == "refused"
        assert engine(receiver).events == []
        assert not (receiver.service.root / "releases").exists()
    else:
        assert result(receiver)["outcome"] == "ok"


def test_busy_space_and_no_previous(receiver: Receiver) -> None:
    staging.prepare(receiver.service)
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


def test_broken_output_does_not_interrupt_receiver(receiver: Receiver) -> None:
    class Broken(io.StringIO):
        @override
        def write(self, _body: str) -> int:
            raise BrokenPipeError

    receiver.out = Broken()
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    command.failed = True
    receiver.line("ok")


def test_recover_then_continue_deploy(receiver: Receiver) -> None:
    staging.prepare(receiver.service)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save(PREVIOUS)
    transaction.write({"token": b"interrupted"})
    assert receiver.run() == 0
    assert isinstance(receiver.out, io.StringIO)
    assert '"outcome": "recovered"' in receiver.out.getvalue()
    assert result(receiver)["outcome"] == "ok"


@pytest.mark.parametrize("reason", [None, "safe refusal"])
def test_exact_result_record(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch, reason: str | None
) -> None:
    receiver.started = 100
    receiver.step = "health"
    monkeypatch.setattr(receiver_module.time, "monotonic", lambda: 102.36)
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


def test_run_exact_progress_and_engine_calls(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver.started = 10
    monkeypatch.setattr(receiver_module.time, "monotonic", lambda: 11.23)
    with (
        patch.object(receiver_io, "stream", wraps=receiver_io.stream) as stream,
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


@pytest.mark.parametrize("failure", [CommandError, OSError])
def test_journal_command_failure_does_not_block_result_output(
    receiver: Receiver, failure: type[Exception]
) -> None:
    with patch.object(receiver.engine, "command", side_effect=failure):
        receiver.line("ok")
    assert result(receiver)["outcome"] == "ok"


def test_run_passes_exact_release_and_stream_limit_to_each_stage(
    receiver: Receiver,
) -> None:
    limit = receiver.service.max_stream_mib * 1024**2
    body = receiver_io.stream(limit)
    transaction = Transaction(receiver.service, receiver.engine)
    with (
        patch.object(receiver_module, "unpack", wraps=receiver_module.unpack) as unpack,
        patch.object(receiver_module, "Transaction", return_value=transaction),
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
    receiver: Receiver,
) -> None:
    with patch.object(Activation, "apply", side_effect=CommandError):
        assert receiver.run() == 3
    assert result(receiver)["outcome"] == "failed"
    assert result(receiver)["reason"] == "receiver step failed"


def test_failure_before_secret_phase_is_a_refusal(receiver: Receiver) -> None:
    with patch.object(receiver.engine, "parse", side_effect=CommandError):
        assert receiver.run() == 2
    assert result(receiver)["outcome"] == "refused"
    assert result(receiver)["reason"] == "receiver step failed"


def test_manual_recovery_restart_failure_retains_journal_and_fails(
    receiver: Receiver,
) -> None:
    staging.prepare(receiver.service)
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
    receiver: Receiver, failure: type[Exception]
) -> None:
    staging.prepare(receiver.service)
    fs.pointer(receiver.service.root / "previous", "releases/" + PREVIOUS)
    with patch.object(Activation, "apply", side_effect=failure):
        assert receiver.run(manual=True) == 3
    assert result(receiver)["outcome"] == "failed"
    assert result(receiver)["reason"] == "recovery failed"


def test_bad_journal_name_reports_without_reading_it_again(
    receiver: Receiver,
) -> None:
    staging.prepare(receiver.service)
    transaction = Transaction(receiver.service, receiver.engine)
    journal = transaction.save(RELEASE)
    journal["current"] = "../outside"
    transaction.update(journal, "open")
    assert receiver.run() == 3
    assert result(receiver)["reason"] == "pointer target"


def test_manual_contents_refuses_missing_previous_with_a_distinct_reason(
    receiver: Receiver,
) -> None:
    staging.prepare(receiver.service)
    with pytest.raises(RefusalError, match=r"^no previous release$"):
        receiver.contents(None)


def test_startup_stops_units_after_preflight_before_release_writes(
    receiver: Receiver,
) -> None:
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    fake: FakeCommand = command
    fake.active = True

    def prepare_after_stop(
        _service: Service, _release_name: str, release: Release
    ) -> None:
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
        patch.object(receiver.staging, "publish", side_effect=prepare_after_stop),
    ):
        assert receiver.run() == 2
    assert result(receiver)["step"] == "release"
    assert result(receiver)["reason"] == "stage checked"


def test_chained_pax_refusal_reports_stream_step(receiver: Receiver) -> None:
    with patch.object(receiver_io, "stream", return_value=chained_pax()):
        assert receiver.run() == 2
    assert result(receiver)["outcome"] == "refused"
    assert result(receiver)["step"] == "stream"
    assert result(receiver)["reason"] == "invalid tar stream"


def test_failed_auto_recovery_retains_journal_until_verified_restart(
    receiver: Receiver,
) -> None:
    seed_previous(receiver)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save(RELEASE)
    transaction.write({"token": b"interrupted"})
    fs.pointer(receiver.service.root / "current", "releases/" + RELEASE)
    daemon = engine(receiver)
    daemon.fail_at = {"health " + PREVIOUS}
    with patch.object(receiver_io, "stream", return_value=b"invalid"):
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


def test_crash_after_health_before_commit_recovers_previous(
    receiver: Receiver,
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


def test_concurrent_receive_is_busy_and_touches_nothing(
    receiver: Receiver,
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    contender = Receiver(
        Request("o11y", "c" * 40, "456"),
        receiver.service,
        ReceiveEngine(receiver.service),
        io.StringIO(),
    )
    seen: dict[str, object] = {}
    original = receiver.engine.run

    def up_with_contender(
        release: str, *arguments: str, timeout_s: float = 300
    ) -> None:
        if arguments[0] == "up":
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            data = pack(
                contender.wanted,
                {"release/compose.yaml": b"model"},
                {"token": b"other"},
            )
            with patch.object(receiver_io, "stream", return_value=data):
                seen["code"] = contender.run()
            seen["unchanged"] = before == {
                str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()
            }
            seen["journal"] = Transaction(receiver.service, receiver.engine).journal()
        original(release, *arguments, timeout_s=timeout_s)

    with patch.object(receiver.engine, "run", side_effect=up_with_contender):
        assert receiver.run() == 0
    assert seen["code"] == 75
    assert result(contender)["outcome"] == "busy"
    assert result(contender)["step"] == "lock"
    assert engine(contender).events == []
    assert seen["unchanged"] is True
    journal = seen["journal"]
    assert isinstance(journal, dict)
    assert journal["state"] == "open"
    assert fs.target(root / "current") == "releases/" + RELEASE
    assert result(receiver)["outcome"] == "ok"
    lock = Lock(root / "deploy")
    lock.acquire()
    lock.release()


def test_manual_rollback_after_committed_crash_still_rolls_back(
    receiver: Receiver,
) -> None:
    seed_previous(receiver)
    root = receiver.service.root

    def crash_after_mark(self: Transaction, journal: dict[str, Json]) -> None:
        self.update(journal, "committed")
        raise KeyboardInterrupt

    with (
        patch.object(Transaction, "finish", crash_after_mark),
        pytest.raises(KeyboardInterrupt),
    ):
        receiver.run()
    transaction = Transaction(receiver.service, receiver.engine)
    journal = transaction.journal()
    assert journal is not None
    assert journal["state"] == "committed"
    assert fs.target(root / "current") == "releases/" + RELEASE
    receiver.staging.created = False
    assert receiver.run(manual=True) == 0
    assert transaction.journal() is None
    assert fs.target(root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    assert (
        transaction.state / "last-secrets" / "payload" / "token"
    ).read_bytes() == b"fixture"


def crash_in_child(receiver: Receiver, point: str) -> None:
    """Kill the receiver with SIGKILL so no finally block or handler can run."""
    if sys.platform != "win32":
        daemon = engine(receiver)
        pointer = Transaction.pointer
        fatal = {
            "after-secrets": "pull " + RELEASE,
            "after-up": "up " + RELEASE,
            "after-health": "health " + RELEASE,
        }

        def dying_pointer(self: Transaction, name: str, target: str | None) -> None:
            pointer(self, name, target)
            if point == "between-pointers" and name == "previous":
                os.kill(os.getpid(), signal.SIGKILL)

        def dying_event(name: str) -> None:
            daemon.events.append(name)
            if fatal.get(point) == name:
                os.kill(os.getpid(), signal.SIGKILL)

        pid = os.fork()
        if pid == 0:
            try:
                with (
                    patch.object(Transaction, "pointer", dying_pointer),
                    patch.object(daemon, "event", side_effect=dying_event),
                ):
                    receiver.run()
            finally:
                os._exit(1)
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == -signal.SIGKILL
    else:
        pytest.skip("Unix signals and fork required")


@pytest.mark.parametrize(
    "point", ["after-secrets", "between-pointers", "after-up", "after-health"]
)
def test_killed_receiver_is_recovered_by_the_next_receive(
    receiver: Receiver, point: str
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    crash_in_child(receiver, point)
    transaction = Transaction(receiver.service, receiver.engine)
    journal = transaction.journal()
    assert journal is not None
    assert journal["state"] == "open"
    assert journal["current"] == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"
    output = io.StringIO()
    fresh = Receiver(
        receiver.wanted, receiver.service, ReceiveEngine(receiver.service), output
    )
    # 10b validates the stream before taking the lock. Stop after recovery,
    # at the next policy pass, so this test observes the recovered service.
    with patch.object(
        fresh.engine, "parse", side_effect=RefusalError("stop here")
    ) as parse:
        assert fresh.run() == 2
    parse.assert_called_once_with(RELEASE)
    assert '"outcome": "recovered"' in output.getvalue()
    assert fs.target(root / "current") == "releases/" + PREVIOUS
    assert fs.target(root / "previous") is None
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    assert transaction.journal() is None
    assert not list((root / "incoming").iterdir())
    events = engine(fresh).events
    assert events.index("stop") < events.index("up " + PREVIOUS)
    assert "force-recreate" in events


def raw_stream(manifest: bytes, *, manifest_first: bool = True) -> bytes:
    members = [
        ("manifest.json", manifest),
        ("release/compose.yaml", b"model"),
        ("secrets/token", b"fixture"),
    ]
    if not manifest_first:
        members.append(members.pop(0))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, body in members:
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return output.getvalue()[: archive.offset]


def manifest(**change: Json) -> bytes:
    value: dict[str, Json] = {
        "format": 1,
        "service": "o11y",
        "release": RELEASE,
        "files": {"release/compose.yaml": sha256(b"model")},
        "secrets": {"token": sha256(b"fixture")},
    }
    value.update(change)
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    ("body", "first", "reason"),
    [
        (b"{not json", True, "invalid JSON"),
        (b"\xff\xfe", True, "invalid JSON"),
        (b'{"format": 1, "format": 1}', True, "invalid JSON"),
        (b"[]", True, "expected an object"),
        (manifest(format="1"), True, "stream format"),
        (manifest(format=True), True, "stream format"),
        (manifest(format=2), True, "stream format"),
        (manifest(service="other"), True, "stream service"),
        (manifest(release=PREVIOUS), True, "stream release"),
        (manifest(extra=1), True, "unknown object key"),
        (manifest(files={}), True, "file manifest mismatch"),
        (
            manifest(files={"release/compose.yaml": "0" * 64}),
            True,
            "file manifest mismatch",
        ),
        (manifest(files=None), True, "file manifest mismatch"),
        (manifest(secrets={"token": "0" * 64}), True, "secret manifest mismatch"),
        (manifest(), False, "manifest first"),
    ],
)
def test_malformed_manifest_is_refused_and_leaves_the_host_untouched(
    receiver: Receiver, body: bytes, first: bool, reason: str
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    lock = Lock(root / "deploy")
    lock.acquire()
    lock.release()
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with patch.object(
        receiver_io, "stream", return_value=raw_stream(body, manifest_first=first)
    ):
        assert receiver.run() == 2
    record = result(receiver)
    assert record["outcome"] == "refused"
    assert record["step"] == "stream"
    assert record["reason"] == reason
    assert {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert not (root / "releases" / RELEASE).exists()
    assert not list((root / "incoming").iterdir())
    assert not (root / "deploy" / "txn").exists()
    assert fs.target(root / "current") == "releases/" + PREVIOUS
    assert engine(receiver).events == []
    lock.acquire()
    lock.release()


def test_raw_manifest_control_deploys(receiver: Receiver) -> None:
    with patch.object(receiver_io, "stream", return_value=raw_stream(manifest())):
        assert receiver.run() == 0
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE


def test_unhealthy_previous_allows_forward_repair_without_losing_recovery_data(
    receiver: Receiver,
) -> None:
    seed_previous(receiver)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save("c" * 40)
    transaction.write({"token": b"broken"})
    engine(receiver).fail_at = {"health " + PREVIOUS}
    with patch.object(receiver_io, "stream", wraps=receiver_io.stream) as stream:
        assert receiver.run() == 0
    stream.assert_called_once()
    assert isinstance(receiver.out, io.StringIO)
    assert '"outcome": "recovered"' not in receiver.out.getvalue()
    assert result(receiver)["outcome"] == "ok"
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"
    assert transaction.journal() is None
    archives = list(transaction.state.glob("recovery-*/journal.json"))
    assert len(archives) == 1
    journal = json.loads(archives[0].read_bytes())
    assert journal["state"] == "restored"
    assert journal["current"] == "releases/" + PREVIOUS
    saved = archives[0].parent / "saved" / "payload" / "token"
    assert saved.read_bytes() == b"earlier"
    # Later pruning must keep every release referenced by the recovery archive.
    for index in range(8):
        (receiver.service.root / "releases" / f"{index:040x}").mkdir()
    fs.pointer(receiver.service.root / "previous", None)
    transaction.prune()
    assert (receiver.service.root / "releases" / PREVIOUS).is_dir()
    assert saved.read_bytes() == b"earlier"


def test_forward_repair_requires_failed_recovery_unit_to_stop(
    receiver: Receiver,
) -> None:
    seed_previous(receiver)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save("c" * 40)
    transaction.write({"token": b"broken"})
    engine(receiver).fail_at = {"health " + PREVIOUS}
    stopped = 0

    def stop_unit() -> None:
        nonlocal stopped
        stopped += 1
        if stopped == 2:
            message = "transient unit stop unconfirmed; compensation withheld"
            raise RefusalError(message)

    with patch.object(receiver.engine, "stop_unit", side_effect=stop_unit):
        assert receiver.run() == 3
    assert stopped == 2
    journal = transaction.journal()
    assert journal is not None
    assert journal["state"] == "restored"
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    assert not list(transaction.state.glob("recovery-*"))
    assert "pull " + RELEASE not in engine(receiver).events

    # Once the failed recovery unit is stopped, the same repair can proceed.
    assert receiver.run() == 0
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"
    assert len(list(transaction.state.glob("recovery-*/journal.json"))) == 1


@pytest.mark.parametrize("number", [getattr(signal, "SIGHUP", 0), signal.SIGTERM])
def test_signal_during_recovery_completes_instead_of_leaving_service_down(
    receiver: Receiver, number: int
) -> None:
    if sys.platform != "win32":
        seed_previous(receiver)
        transaction = Transaction(receiver.service, receiver.engine)
        transaction.save(RELEASE)
        transaction.write({"token": b"interrupted"})
        original = receiver.engine.up

        def interrupted_up(release: str, *, recreate: bool) -> None:
            os.kill(os.getpid(), number)
            original(release, recreate=recreate)

        pid = os.fork()
        if pid == 0:
            try:
                signal.signal(number, signal.SIG_DFL)
                with patch.object(receiver.engine, "up", side_effect=interrupted_up):
                    code = receiver.run(manual=True)
                os._exit(code)
            finally:
                os._exit(99)
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
        assert transaction.journal() is None
        assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
        assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    else:
        pytest.skip("Unix signals and fork required")
