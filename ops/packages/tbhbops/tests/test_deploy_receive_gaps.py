"""Receiver gaps: lock ordering, crash boundaries, nested rollback, bad manifests."""

import io
import json
import os
import signal
import sys
import tarfile
from unittest.mock import patch

import pytest
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy import receive
from tbhbops.deploy.protocol import Json, RefusalError, Request, pack, sha256
from tbhbops.deploy.transaction import Lock, Transaction

from .test_deploy_config import deploy_service as deploy_service  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_filesystem import trusted_tree as trusted_tree  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_receive import (
    PREVIOUS,
    RELEASE,
    ReceiveEngine,
    engine,
    result,
    seed_previous,
)
from .test_deploy_receive import receiver as receiver  # noqa: PLC0414  # pytest fixture registration


def test_concurrent_receive_is_busy_and_touches_nothing(
    receiver: receive.Receiver,
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    contender = receive.Receiver(
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
            with patch.object(receive, "stream", return_value=data):
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
    receiver: receive.Receiver,
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
    receiver.created = False
    assert receiver.run(manual=True) == 0
    assert transaction.journal() is None
    assert fs.target(root / "current") == "releases/" + PREVIOUS
    assert receiver.service.secrets[0].target.read_bytes() == b"earlier"
    assert (
        transaction.state / "last-secrets" / "payload" / "token"
    ).read_bytes() == b"fixture"


def crash_in_child(receiver: receive.Receiver, point: str) -> None:
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
    receiver: receive.Receiver, point: str
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
    fresh = receive.Receiver(
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


@pytest.mark.parametrize("failure", ["up", "health"])
def test_failed_manual_rollback_returns_to_the_newer_release(
    receiver: receive.Receiver, failure: str
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    assert receiver.run() == 0
    receiver.created = False
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
    receiver: receive.Receiver,
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    assert receiver.run() == 0
    receiver.created = False
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
    with patch.object(receive, "stream", return_value=b"invalid") as stream:
        assert receiver.run(manual=True) == 0
    stream.assert_not_called()
    assert transaction.journal() is None
    assert fs.target(root / "current") == "releases/" + RELEASE
    assert receiver.service.secrets[0].target.read_bytes() == b"fixture"


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
    receiver: receive.Receiver, body: bytes, first: bool, reason: str
) -> None:
    seed_previous(receiver)
    root = receiver.service.root
    lock = Lock(root / "deploy")
    lock.acquire()
    lock.release()
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with patch.object(
        receive, "stream", return_value=raw_stream(body, manifest_first=first)
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


def test_raw_manifest_control_deploys(receiver: receive.Receiver) -> None:
    with patch.object(receive, "stream", return_value=raw_stream(manifest())):
        assert receiver.run() == 0
    assert fs.target(receiver.service.root / "current") == "releases/" + RELEASE
