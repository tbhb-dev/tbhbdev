"""Verify live activation, compensation, and rollback."""

import signal
from dataclasses import replace
from typing import override
from unittest.mock import Mock, call, patch
lazy from pathlib import Path

import pytest
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy import receiver_io, staging
from tbhbops.deploy.activation import Activation
from tbhbops.deploy.compose import Engine
from tbhbops.deploy.protocol import Json, RefusalError, Request, pack
from tbhbops.deploy.receiver import Receiver
from tbhbops.deploy.transaction import Transaction

from ...deploy_fakes import FakeCommand
from ...deploy_model import RELEASE
from ...deploy_receiver import (
    PREVIOUS,
    RetryCommand,
    align_secret_group,
    engine,
    result,
    seed_previous,
    use_health_clock,
)


def activation(receiver: Receiver, transaction: Transaction) -> Activation:
    return Activation(
        transaction, receiver.wanted.release, receiver.signals, receiver.progress
    )


def test_success_and_identical_release(receiver: Receiver) -> None:
    assert receiver.run() == 0
    assert result(receiver)["outcome"] == "ok"
    assert result(receiver)["step"] == "done"
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"
    assert not (receiver.service.root / "deploy" / "txn").exists()
    assert "force-recreate" in engine(receiver).events
    receiver.staging.incoming = None
    receiver.staging.created = False
    assert receiver.run() == 0
    assert (receiver.service.root / "deploy" / "lock").is_file()


@pytest.mark.parametrize(
    "failure", ["pull " + RELEASE, "up " + RELEASE, "health " + RELEASE]
)
def test_compensation(receiver: Receiver, failure: str) -> None:
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


def test_no_prior_release_and_failed_rollback(receiver: Receiver) -> None:
    engine(receiver).fail_at = {"up " + RELEASE}
    assert receiver.run() == 3
    assert result(receiver)["outcome"] == "failed"
    assert fs.target(receiver.service.root / "current") is None
    assert not receiver.service.secrets[0].target.exists()
    receiver.staging.created = False
    seed_previous(receiver)
    engine(receiver).fail_at.add("health " + PREVIOUS)
    assert receiver.run() == 3
    assert (receiver.service.root / "deploy" / "txn" / "journal.json").exists()
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS


def test_manual_rollback_and_recovery_only(receiver: Receiver) -> None:
    seed_previous(receiver)
    assert receiver.run() == 0
    receiver.staging.created = False
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


def test_signal_compensates(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = receiver.engine.run

    def interrupted(release: str, *args: str) -> None:
        original(release, *args)
        receiver.signals.stop(signal.SIGTERM, None)

    monkeypatch.setattr(receiver.engine, "run", interrupted)
    assert receiver.run() == 4
    assert not receiver.service.secrets[0].target.exists()


def test_init_and_unchanged_secret_skip_recreate(receiver: Receiver) -> None:
    executable = receiver.service.root / "init"
    executable.write_bytes(b"fixture")
    receiver.service = replace(receiver.service, init=(str(executable),))
    assert receiver.run() == 0
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    assert ([str(executable)], None) in command.calls
    receiver.staging.incoming = None
    receiver.staging.created = False
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
    receiver.staging.created = False
    receiver.staging.incoming = None
    assert receiver.run() == 0
    assert "force-recreate" in engine(receiver).events
    assert not marker.exists()


def test_rollback_refuses_a_missing_transaction(receiver: Receiver) -> None:
    transaction = Transaction(receiver.service, receiver.engine)
    with (
        patch.object(transaction, "journal", return_value=None),
        pytest.raises(RefusalError, match=r"^missing transaction$"),
    ):
        activation(receiver, transaction).rollback()


def test_rollback_recreates_the_recorded_release(receiver: Receiver) -> None:
    seed_previous(receiver)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save(RELEASE)
    with (
        patch.object(receiver, "progress", wraps=receiver.progress) as progress,
        patch.object(receiver.engine, "up", wraps=receiver.engine.up) as up,
    ):
        assert activation(receiver, transaction).rollback() == (1, "rolled_back")
    progress.assert_called_once_with("rollback")
    up.assert_called_once_with(PREVIOUS, recreate=True)


def test_rollback_without_prior_release_returns_failed_status(
    receiver: Receiver,
) -> None:
    transaction = Mock()
    journal: dict[str, Json] = {"current": None}
    transaction.journal.return_value = journal
    assert activation(receiver, transaction).rollback() == (3, "failed")
    transaction.delete.assert_called_once_with()


@pytest.mark.parametrize("failure", ["pull", "up", "health"])
def test_compensation_waits_for_units_or_retains_journal(
    receiver: Receiver, failure: str
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
    receiver: Receiver, failure: str
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
    receiver: Receiver,
) -> None:
    align_secret_group(receiver)
    wanted = receiver.wanted
    receiver.wanted = Request(wanted.service, PREVIOUS, None)
    body = pack(
        receiver.wanted,
        {"release/compose.yaml": b"previous model"},
        {"token": b"earlier"},
    )
    with patch.object(receiver_io, "stream", return_value=body):
        assert receiver.run() == 0
    receiver.wanted = wanted
    receiver.staging.created = False
    assert receiver.run() == 0
    transaction = Transaction(receiver.service, receiver.engine)
    saved = transaction.state / "last-secrets"
    snapshot = {
        path.relative_to(saved): path.read_bytes()
        for path in saved.rglob("*")
        if path.is_file()
    }
    receiver.staging.created = False
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


def retry_restarts(receiver: Receiver, stopped: str) -> None:
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
    retry_receiver: Receiver,
) -> None:
    retry_restarts(retry_receiver, "worker")


def test_identical_retry_restarts_stopped_health_gated_service(
    retry_receiver: Receiver,
) -> None:
    retry_restarts(retry_receiver, "web")


@pytest.mark.parametrize("failure", ["up", "health"])
def test_identical_retry_failure_preserves_current_and_rollback_state(
    retry_receiver: Receiver, failure: str
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


@pytest.mark.parametrize("failure", [ValueError, KeyError, TypeError, RuntimeError])
@pytest.mark.parametrize("step", ["pull", "up", "health"])
def test_unexpected_apply_failure_compensates(
    receiver: Receiver, failure: type[Exception], step: str
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


def test_new_release_with_unchanged_secrets_uses_normal_compose_up(
    receiver: Receiver,
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
    receiver: Receiver,
) -> None:
    staging.prepare(receiver.service)
    transaction = Transaction(receiver.service, receiver.engine)
    change = activation(receiver, transaction)
    with (
        patch.object(
            receiver.engine,
            "health",
            side_effect=RefusalError("unit stop unconfirmed; compensation withheld"),
        ),
        patch.object(change, "rollback") as rollback,
        pytest.raises(RefusalError, match="compensation withheld"),
    ):
        change.apply({"token": b"candidate"}, manual=False)
    rollback.assert_not_called()
    journal = transaction.journal()
    assert journal is not None
    assert journal["state"] == "open"
    assert receiver.service.secrets[0].target.read_bytes() == b"candidate"


@pytest.mark.parametrize("local", [False, True])
def test_manual_rollback_pulls_only_missing_images(
    receiver: Receiver, local: bool
) -> None:
    seed_previous(receiver)
    assert receiver.run() == 0
    receiver.staging.created = False
    engine(receiver).events.clear()
    with patch.object(receiver.engine, "local_images", return_value=local) as images:
        assert receiver.run(manual=True) == 0
    images.assert_called_once_with(PREVIOUS)
    assert ("pull " + PREVIOUS in engine(receiver).events) is not local
    assert "up " + PREVIOUS in engine(receiver).events


@pytest.mark.parametrize("failure", ["up", "health"])
def test_failed_manual_rollback_returns_to_the_newer_release(
    receiver: Receiver, failure: str
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    assert receiver.run() == 0
    receiver.staging.created = False
    transaction = Transaction(receiver.service, receiver.engine)
    last = transaction.state / "last-secrets" / "payload" / "token"
    assert last.read_bytes() == b"earlier"
    daemon = engine(receiver)
    daemon.events.clear()
    daemon.fail_at = {failure + " " + PREVIOUS}
    assert receiver.run(manual=True) == 1
    record = result(receiver)
    assert record["outcome"] == "rolled_back"
    assert record["step"] == "rollback"
    assert record["release"] == PREVIOUS
    assert fs.target(root / "current") == "releases/" + RELEASE
    assert fs.target(root / "previous") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"
    assert transaction.journal() is None
    assert last.read_bytes() == b"earlier"
    assert (root / "releases" / RELEASE).is_dir()
    assert (root / "releases" / PREVIOUS).is_dir()
    assert daemon.events.index("stop") < daemon.events.index("up " + RELEASE)
    daemon.fail_at.clear()
    assert receiver.run(manual=True) == 0
    assert fs.target(root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    assert receiver.run(manual=True) == 0
    assert fs.target(root / "current") == "releases/" + RELEASE
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"


def test_manual_rollback_whose_compensation_fails_keeps_the_journal(
    receiver: Receiver,
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    assert receiver.run() == 0
    receiver.staging.created = False
    daemon = engine(receiver)
    daemon.fail_at = {"health " + PREVIOUS, "up " + RELEASE}
    assert receiver.run(manual=True) == 3
    assert result(receiver)["outcome"] == "failed"
    assert result(receiver)["reason"] == "recovery failed"
    transaction = Transaction(receiver.service, receiver.engine)
    journal = transaction.journal()
    assert journal is not None
    assert journal["state"] == "restored"
    assert journal["current"] == "releases/" + RELEASE
    daemon.fail_at.clear()
    with patch.object(receiver_io, "stream", return_value=b"invalid") as stream:
        assert receiver.run(manual=True) == 0
    stream.assert_not_called()
    assert transaction.journal() is None
    assert fs.target(root / "current") == "releases/" + RELEASE
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"


@pytest.mark.parametrize("difference", ["marker", "mode", "uid", "gid"])
def test_current_replay_after_aborted_candidate_preserves_rollback_pair(
    receiver: Receiver, difference: str
) -> None:
    secret = receiver.service.secrets[0]
    align_secret_group(receiver)

    def deploy(release: str, body: bytes) -> int:
        receiver.staging.created = False
        receiver.wanted = Request("o11y", release, "123")
        data = pack(
            receiver.wanted, {"release/compose.yaml": release.encode()}, {"token": body}
        )
        with patch.object(receiver_io, "stream", return_value=data):
            return receiver.run()

    assert deploy(PREVIOUS, b"B") == 0
    assert deploy(RELEASE, b"A") == 0
    saved = receiver.service.root / "deploy" / "last-secrets"
    before = {
        p.relative_to(saved): p.read_bytes() for p in saved.rglob("*") if p.is_file()
    }
    candidate = "c" * 40
    engine(receiver).fail_at = {"pull " + candidate}
    assert deploy(candidate, b"C") == 4
    engine(receiver).fail_at.clear()
    marker = receiver.service.root / "deploy" / "force-recreate"
    if difference != "marker":
        marker.unlink()
        if difference == "mode":
            secret.target.chmod(0o600)
        else:
            receiver.service = replace(
                receiver.service,
                secrets=(
                    replace(
                        receiver.service.secrets[0],
                        uid=1234
                        if difference == "uid"
                        else receiver.service.secrets[0].uid,
                        gid=1234
                        if difference == "gid"
                        else receiver.service.secrets[0].gid,
                    ),
                ),
            )
            receiver.engine.service = receiver.service
    assert deploy(RELEASE, b"A") == 0
    assert fs.target(receiver.service.root / "previous") == "releases/" + PREVIOUS
    assert {
        p.relative_to(saved): p.read_bytes() for p in saved.rglob("*") if p.is_file()
    } == before
    assert not marker.exists()
    assert receiver.run(manual=True) == 0
    assert fs.target(receiver.service.root / "current") == "releases/" + PREVIOUS
    assert secret.target.read_bytes() == b"B"


def test_identical_retry_failure_keeps_recoverable_journal(
    retry_receiver: Receiver,
) -> None:
    receiver = retry_receiver
    command = receiver.engine.command
    assert isinstance(command, RetryCommand)
    command.running["web"] = False
    command.fail_health = True
    assert receiver.run() == 3
    transaction = Transaction(receiver.service, receiver.engine)
    journal = transaction.journal()
    assert journal is not None
    assert (journal["state"], journal["current"]) == ("restored", "releases/" + RELEASE)
    command.fail_health = False
    assert receiver.run(manual=True) == 0
    assert transaction.journal() is None
    assert command.running == {"web": True, "worker": True}
    assert fs.target(receiver.service.root / "previous") == "releases/" + PREVIOUS


@pytest.mark.parametrize("operation", ["deploy", "recover"])
def test_failed_first_deploy_leaves_nothing_a_daemon_restart_can_revive(
    receiver: Receiver, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    align_secret_group(receiver)
    receiver.service = replace(receiver.service, timeout_s=1)
    command = RemovalCommand()
    receiver.engine = Engine(receiver.service, "/compose", command)
    use_health_clock(receiver.engine, monkeypatch)
    command.fail_health = True
    if operation == "deploy":
        assert receiver.run() == 3
    else:
        staging.prepare(receiver.service)
        transaction = Transaction(receiver.service, receiver.engine)
        transaction.save(RELEASE)
        transaction.write({"token": b"candidate"})
        assert transaction.recover()
    assert command.running == {}
    # restart: always applies to every container left in the daemon, even stopped ones.
    command.running = dict.fromkeys(command.running, True)
    assert not any(command.running.values())
    assert not receiver.service.secrets[0].target.exists()
    assert Transaction(receiver.service, receiver.engine).journal() is None


class RemovalCommand(RetryCommand):
    @override
    def __call__(
        self, argv: list[str], data: bytes | None = None, **kwargs: object
    ) -> bytes:
        output = super().__call__(argv, data, **kwargs)
        if argv[:2] == ["/usr/bin/docker", "stop"]:
            self.running = dict.fromkeys(self.running, False)
        if argv[:2] == ["/usr/bin/docker", "rm"]:
            assert argv[2:] == ["-f", "abc"]
            self.running.clear()
        return output


def test_identical_retry_does_not_rerun_init(receiver: Receiver) -> None:
    init = str(Path("/usr/bin/true").resolve())
    secret = receiver.service.secrets[0]
    receiver.service = replace(
        receiver.service,
        secrets=(replace(secret, gid=secret.target.parent.stat().st_gid),),
        init=(init,),
    )
    receiver.engine.service = receiver.service
    assert receiver.run() == 0
    receiver.staging.created = False
    assert receiver.run() == 0
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    assert command.calls.count(([init], None)) == 1
    assert engine(receiver).events.count("up " + RELEASE) == 2
