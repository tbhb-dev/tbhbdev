"""Regressions from the receiver review, using durable transactions."""

import io
import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch

import pytest

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy import receive
from tbhbops.deploy.engine import Engine
from tbhbops.deploy.protocol import RefusalError, Request, pack
from tbhbops.deploy.transaction import Transaction

from .test_deploy_config import deploy_service as deploy_service  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_engine import FakeCommand
from .test_deploy_filesystem import trusted_tree as trusted_tree  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_receive import (
    PREVIOUS,
    RELEASE,
    RetryCommand,
    align_secret_group,
    engine,
    result,
    seed_previous,
    use_health_clock,
)
from .test_deploy_receive import receiver as receiver  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_receive import retry_receiver as retry_receiver  # noqa: PLC0414  # pytest fixture registration


@pytest.mark.parametrize("difference", ["marker", "mode", "uid", "gid"])
def test_current_replay_after_aborted_candidate_preserves_rollback_pair(
    receiver: receive.Receiver, difference: str
) -> None:
    secret = receiver.service.secrets[0]
    align_secret_group(receiver)

    def deploy(release: str, body: bytes) -> int:
        receiver.created = False
        receiver.wanted = Request("o11y", release, "123")
        data = pack(
            receiver.wanted, {"release/compose.yaml": release.encode()}, {"token": body}
        )
        with patch.object(receive, "stream", return_value=data):
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
    retry_receiver: receive.Receiver,
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


def test_unhealthy_previous_allows_forward_repair_without_losing_recovery_data(
    receiver: receive.Receiver,
) -> None:
    seed_previous(receiver)
    transaction = Transaction(receiver.service, receiver.engine)
    transaction.save("c" * 40)
    transaction.write({"token": b"broken"})
    engine(receiver).fail_at = {"health " + PREVIOUS}
    with patch.object(receive, "stream", wraps=receive.stream) as stream:
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
    receiver: receive.Receiver,
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


@pytest.mark.parametrize("operation", ["deploy", "recover"])
def test_failed_first_deploy_leaves_nothing_a_daemon_restart_can_revive(
    receiver: receive.Receiver, operation: str, monkeypatch: pytest.MonkeyPatch
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
        receiver.prepare()
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


@pytest.mark.parametrize("number", [getattr(signal, "SIGHUP", 0), signal.SIGTERM])
def test_signal_during_recovery_completes_instead_of_leaving_service_down(
    receiver: receive.Receiver, number: int
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


def test_identical_retry_does_not_rerun_init(receiver: receive.Receiver) -> None:
    init = str(Path("/usr/bin/true").resolve())
    secret = receiver.service.secrets[0]
    receiver.service = replace(
        receiver.service,
        secrets=(replace(secret, gid=secret.target.parent.stat().st_gid),),
        init=(init,),
    )
    receiver.engine.service = receiver.service
    assert receiver.run() == 0
    receiver.created = False
    assert receiver.run() == 0
    command = receiver.engine.command
    assert isinstance(command, FakeCommand)
    assert command.calls.count(([init], None)) == 1
    assert engine(receiver).events.count("up " + RELEASE) == 2
