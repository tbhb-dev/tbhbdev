"""Real-file transaction tests exercise durable restore and retry ordering."""

import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import replace
from typing import override
from unittest.mock import Mock, call, patch
lazy from pathlib import Path

import pytest

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.engine import Command, CommandError, Engine
from tbhbops.deploy.protocol import Json, RefusalError
from tbhbops.deploy.transaction import BusyError, Lock, Transaction
lazy from tbhbops.deploy.config import Service

from .test_deploy_config import deploy_service as deploy_service  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_filesystem import trusted_tree as trusted_tree  # noqa: PLC0414  # pytest fixture registration


class FakeEngine(Engine):
    def __init__(self, service: Service) -> None:
        super().__init__(service, "/compose", Command())
        self.calls: list[str] = []
        self.fail_up = False

    @override
    def stop(self) -> None:
        self.calls.append("stop")

    @override
    def remove(self) -> None:
        self.calls.append("remove")

    @override
    def up(self, release: str, *, recreate: bool) -> None:
        assert recreate
        self.calls.append("up " + release)
        if self.fail_up:
            raise CommandError


@pytest.fixture
def transaction(deploy_service: Service, trusted_tree: Path) -> Transaction:
    root = trusted_tree
    secret = replace(
        deploy_service.secrets[0], target=root / "data" / "secrets" / "token"
    )
    service = replace(deploy_service, root=root, secrets=(secret,))
    for path in (
        root / "deploy",
        root / "incoming",
        root / "releases",
        root / "deploy" / "models",
        root / "data",
        root / "data" / "secrets",
    ):
        path.mkdir(mode=0o700)
    return Transaction(service, FakeEngine(service))


def test_save_restore_unset(transaction: Transaction) -> None:
    journal = transaction.save("a" * 40)
    assert journal["state"] == "open"
    target = transaction.service.secrets[0].target
    assert transaction.write({"token": b"first"})
    transaction.pointer("current", "releases/" + "a" * 40)
    transaction.restore(journal)
    assert not target.exists()
    assert fs.target(transaction.service.root / "current") is None
    assert fs.target(transaction.service.root / "previous") is None
    assert (transaction.state / "force-recreate").read_bytes() == b"1\n"
    transaction.restore(journal)
    assert transaction.journal() is not None
    transaction.delete()
    assert not transaction.txn.exists()


def test_save_restore_metadata(transaction: Transaction) -> None:
    secret = transaction.service.secrets[0]
    fs.write(secret.target, b"old", secret.mode, secret.uid, secret.gid)
    transaction.pointer("current", "releases/" + "b" * 40)
    transaction.pointer("previous", "releases/" + "c" * 40)
    journal = transaction.save("a" * 40)
    assert transaction.write({"token": b"new"})
    transaction.restore(journal)
    assert secret.target.read_bytes() == b"old"
    assert fs.target(transaction.service.root / "current") == "releases/" + "b" * 40
    assert fs.target(transaction.service.root / "previous") == "releases/" + "c" * 40
    transaction.delete()


def test_unchanged_and_metadata_change(transaction: Transaction) -> None:
    secret = transaction.service.secrets[0]
    fs.write(secret.target, b"old", secret.mode)
    # os.stat is not the descriptor fstat credential adapter.
    transaction.service = replace(
        transaction.service,
        secrets=(replace(secret, uid=0, gid=secret.target.stat().st_gid),),
    )
    assert not transaction.write({"token": b"old"})
    secret.target.chmod(0o600)
    assert transaction.write({"token": b"old"})


def test_commit_recovery_and_second_finish(transaction: Transaction) -> None:
    root = transaction.service.root
    transaction.txn.mkdir()
    (transaction.txn / "unfinished").write_bytes(b"x")
    assert not transaction.recover()
    assert not transaction.txn.exists()
    journal = transaction.save("a" * 40)
    transaction.write({"token": b"first"})
    transaction.update(journal, "committed")
    assert not transaction.recover()
    assert (transaction.state / "last-secrets" / "metadata" / "metadata.json").exists()
    assert transaction.journal() is None
    journal = transaction.save("b" * 40)
    transaction.write({"token": b"second"})
    transaction.finish(journal)
    assert (
        transaction.state / "last-secrets" / "payload" / "token"
    ).read_bytes() == b"first"
    journal = transaction.save("c" * 40)
    transaction.write({"token": b"third"})
    assert transaction.write_saved()
    assert transaction.service.secrets[0].target.read_bytes() == b"first"
    transaction.finish(journal)
    # A crash after moving saved/ can replay finish without losing last-secrets/.
    transaction.txn.mkdir()
    transaction.update(journal, "committed")
    transaction.finish(journal)
    assert (
        transaction.state / "last-secrets" / "payload" / "token"
    ).read_bytes() == b"second"
    assert not list((root / "incoming").iterdir())


def test_manual_absent_secret(transaction: Transaction) -> None:
    journal = transaction.save("a" * 40)
    transaction.write({"token": b"first"})
    transaction.finish(journal)
    transaction.save("b" * 40)
    assert transaction.write_saved()
    assert not transaction.service.secrets[0].target.exists()


@pytest.mark.parametrize("fail_up", [False, True])
def test_open_recovery_stops_before_restore(
    transaction: Transaction, fail_up: bool
) -> None:
    transaction.pointer("current", "releases/" + "b" * 40)
    journal = transaction.save("a" * 40)
    transaction.write({"token": b"new"})
    engine = transaction.engine
    assert isinstance(engine, FakeEngine)
    engine.fail_up = fail_up
    if fail_up:
        with pytest.raises(RefusalError, match=r"^recovery still pending$"):
            transaction.recover()
    else:
        assert transaction.recover()
    assert engine.calls == ["stop", "up " + "b" * 40]
    assert transaction.txn.exists() is fail_up
    assert fs.target(transaction.service.root / "current") == journal["current"]
    assert (transaction.state / "force-recreate").exists()


def test_restore_failure_keeps_journal(
    transaction: Transaction, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = transaction.save("a" * 40)
    transaction.write({"token": b"new"})

    def fail(_path: Path) -> None:
        message = "injected restore failure"
        raise OSError(message)

    monkeypatch.setattr(fs, "unlink", fail)
    with pytest.raises(OSError, match="injected restore failure"):
        transaction.restore(journal)
    assert transaction.journal() is not None


def seed_releases(transaction: Transaction, order: tuple[int, ...]) -> None:
    for rank, number in enumerate(order, 1):
        release = f"{number:040x}"
        directory = transaction.service.root / "releases" / release
        directory.mkdir()
        os.utime(directory, ns=(rank, rank))
        fs.write(transaction.engine.model_path(release), b"{}")


def test_prune_retains_five_and_pointers(transaction: Transaction) -> None:
    root = transaction.service.root
    seed_releases(transaction, (7, 0, 2, 5, 1, 6, 3, 4))
    transaction.pointer("current", "releases/" + f"{2:040x}")
    transaction.pointer("previous", "releases/" + f"{7:040x}")
    (root / "incoming" / "old").mkdir()
    transaction.prune()
    assert {path.name for path in (root / "releases").iterdir()} == {
        f"{number:040x}" for number in (1, 2, 3, 4, 5, 6, 7)
    }
    assert not transaction.engine.model_path(f"{0:040x}").exists()
    assert {path.name for path in (root / "deploy" / "models").iterdir()} == {
        f"{number:040x}.json" for number in (1, 2, 3, 4, 5, 6, 7)
    }
    assert not list((root / "incoming").iterdir())


def test_save_discards_incomplete_directory(transaction: Transaction) -> None:
    transaction.txn.mkdir()
    (transaction.txn / "partial").write_bytes(b"fixture")
    transaction.save("a" * 40)
    assert not (transaction.txn / "partial").exists()
    with pytest.raises(ValueError, match=r"^pending transaction$"):
        transaction.save("b" * 40)


def test_recovery_without_previous_or_secrets(transaction: Transaction) -> None:
    transaction.service = replace(transaction.service, secrets=())
    transaction.save("a" * 40)
    assert transaction.recover()
    assert not transaction.txn.exists()
    assert not (transaction.state / "force-recreate").exists()


@pytest.mark.parametrize("difference", ["none", "absent", "body", "mode", "uid", "gid"])
def test_secret_write_each_metadata_difference(
    transaction: Transaction, difference: str
) -> None:
    secret = replace(transaction.service.secrets[0], uid=123, gid=456)
    transaction.service = replace(transaction.service, secrets=(secret,))
    mode = 0o600 if difference == "mode" else secret.mode
    uid = 124 if difference == "uid" else secret.uid
    gid = 457 if difference == "gid" else secret.gid
    info = (
        None
        if difference == "absent"
        else os.stat_result((0o100000 | mode, 0, 0, 0, uid, gid, 0, 0, 0, 0))
    )
    with (
        patch.object(fs, "inspect", return_value=info) as inspect,
        patch.object(
            fs, "read", return_value=b"old" if difference == "body" else b"new"
        ) as read,
        patch.object(fs, "write") as write,
    ):
        assert transaction.write({"token": b"new"}) is (difference != "none")
    inspect.assert_called_once_with(secret.target, file=True, missing=True)
    if difference == "absent":
        read.assert_not_called()
    else:
        read.assert_called_once_with(secret.target)
    if difference == "none":
        write.assert_not_called()
    else:
        write.assert_called_once_with(secret.target, b"new", secret.mode, 123, 456)


def test_saved_journal_and_backup_metadata(transaction: Transaction) -> None:
    secret = transaction.service.secrets[0]
    fs.write(secret.target, b"before", 0o640)
    transaction.pointer("current", "releases/" + "b" * 40)
    transaction.pointer("previous", "releases/" + "c" * 40)
    metadata: dict[str, Json] = {
        "token": {
            "existed": True,
            "mode": 0o640,
            "uid": 0,
            "gid": secret.target.stat().st_gid,
        }
    }
    expected: dict[str, Json] = {
        "state": "open",
        "release": "a" * 40,
        "current": "releases/" + "b" * 40,
        "previous": "releases/" + "c" * 40,
        "secrets": metadata,
    }
    with patch.object(fs, "sync", wraps=fs.sync) as sync:
        assert transaction.save("a" * 40) == expected
    assert sync.call_args_list == [
        call(transaction.txn / "saved"),
        call(transaction.txn),
        call(transaction.state),
    ]
    assert (transaction.txn / "journal.json").read_bytes() == json.dumps(
        expected, sort_keys=True
    ).encode()
    assert (
        transaction.txn / "saved" / "metadata" / "metadata.json"
    ).read_bytes() == json.dumps(metadata, sort_keys=True).encode()
    assert (transaction.txn / "saved" / "payload" / "token").read_bytes() == b"before"
    assert transaction.journal() == expected
    transaction.update(expected, "restored")
    assert expected["state"] == "restored"
    assert transaction.journal() == expected


@pytest.mark.parametrize("existed", [False, True])
def test_write_saved_preserves_recorded_metadata(
    transaction: Transaction, existed: bool
) -> None:
    saved = transaction.state / "last-secrets"
    entries = {"token": {"existed": existed, "mode": 0o640, "uid": 123, "gid": 456}}
    with (
        patch.object(
            fs, "read", side_effect=[json.dumps(entries).encode(), b"old"]
        ) as read,
        patch.object(fs, "write") as write,
        patch.object(fs, "unlink") as unlink,
    ):
        assert transaction.write_saved() is True
    if existed:
        assert read.call_args_list == [
            call(saved / "metadata" / "metadata.json"),
            call(saved / "payload" / "token"),
        ]
        assert write.call_args_list == [
            call(transaction.state / "force-recreate", b"1\n"),
            call(transaction.service.secrets[0].target, b"old", 0o640, 123, 456),
        ]
        unlink.assert_not_called()
    else:
        read.assert_called_once_with(saved / "metadata" / "metadata.json")
        write.assert_called_once_with(transaction.state / "force-recreate", b"1\n")
        unlink.assert_called_once_with(transaction.service.secrets[0].target)


def test_empty_saved_set_returns_false(transaction: Transaction) -> None:
    transaction.service = replace(transaction.service, secrets=())
    with (
        patch.object(fs, "read", return_value=b"{}"),
        patch.object(fs, "write") as write,
    ):
        assert transaction.write_saved() is False
    write.assert_called_once_with(transaction.state / "force-recreate", b"1\n")


@pytest.mark.parametrize("state", ["", "OPEN", "closed"])
def test_recovery_rejects_unknown_journal_state(
    transaction: Transaction, state: str
) -> None:
    journal = transaction.save("a" * 40)
    transaction.update(journal, state)
    with pytest.raises(RefusalError, match=r"^journal state$"):
        transaction.recover()
    assert transaction.journal() == journal


def test_restore_exact_metadata_and_pointers(transaction: Transaction) -> None:
    journal: dict[str, Json] = {
        "state": "open",
        "release": "a" * 40,
        "current": "releases/" + "b" * 40,
        "previous": None,
        "secrets": {"token": {"existed": True, "mode": 0o640, "uid": 123, "gid": 456}},
    }
    with (
        patch.object(fs, "read", return_value=b"prior") as read,
        patch.object(fs, "write") as write,
        patch.object(transaction, "pointer") as pointer,
        patch.object(transaction, "update") as update,
    ):
        transaction.restore(journal)
    read.assert_called_once_with(transaction.txn / "saved" / "payload" / "token")
    assert write.call_args_list == [
        call(transaction.state / "force-recreate", b"1\n"),
        call(transaction.service.secrets[0].target, b"prior", 0o640, 123, 456),
    ]
    assert pointer.call_args_list == [
        call("current", "releases/" + "b" * 40),
        call("previous", None),
    ]
    update.assert_called_once_with(journal, "restored")


def test_finish_marks_committed_before_cleanup(transaction: Transaction) -> None:
    journal = transaction.save("a" * 40)
    with (
        patch.object(transaction, "update", wraps=transaction.update) as update,
        patch.object(transaction, "prune", wraps=transaction.prune) as prune,
        patch.object(transaction, "delete", wraps=transaction.delete) as delete,
    ):
        manager = Mock()
        manager.attach_mock(update, "update")
        manager.attach_mock(prune, "prune")
        manager.attach_mock(delete, "delete")
        transaction.finish(journal)
    assert manager.mock_calls == [
        call.update(journal, "committed"),
        call.prune(),
        call.delete(),
    ]
    assert journal["state"] == "committed"


@pytest.mark.parametrize("failure", [CommandError, RefusalError])
def test_restored_recovery_retains_journal_after_failed_up(
    transaction: Transaction, failure: type[Exception]
) -> None:
    transaction.pointer("current", "releases/" + "b" * 40)
    journal = transaction.save("a" * 40)
    transaction.restore(journal)
    with (
        patch.object(transaction.engine, "up", side_effect=failure) as up,
        pytest.raises(RefusalError, match=r"^recovery still pending$"),
    ):
        transaction.recover()
    up.assert_called_once_with("b" * 40, recreate=True)
    assert transaction.journal() is not None


def test_save_requires_regular_secret_targets(transaction: Transaction) -> None:
    transaction.service.secrets[0].target.mkdir()
    with pytest.raises(RefusalError, match=r"^path type$"):
        transaction.save("a" * 40)


def test_delete_unlinks_journal_before_removing_transaction(
    transaction: Transaction,
) -> None:
    transaction.save("a" * 40)
    with (
        patch.object(fs, "unlink", wraps=fs.unlink) as unlink,
        patch.object(shutil, "rmtree", wraps=shutil.rmtree) as rmtree,
        patch.object(fs, "sync", wraps=fs.sync) as sync,
    ):
        manager = Mock()
        manager.attach_mock(unlink, "unlink")
        manager.attach_mock(rmtree, "rmtree")
        manager.attach_mock(sync, "sync")
        transaction.delete()
    assert manager.mock_calls == [
        call.unlink(transaction.txn / "journal.json"),
        call.rmtree(transaction.txn),
        call.sync(transaction.state),
    ]
    assert not transaction.txn.exists()


def test_prune_keeps_exactly_five_unpointed_releases(transaction: Transaction) -> None:
    root = transaction.service.root
    seed_releases(transaction, tuple(range(8)))
    (root / "incoming" / "abandoned").mkdir()
    transaction.prune()
    assert {path.name for path in (root / "releases").iterdir()} == {
        f"{number:040x}" for number in range(3, 8)
    }
    assert {path.name for path in (root / "deploy" / "models").iterdir()} == {
        f"{number:040x}.json" for number in range(3, 8)
    }
    assert list((root / "incoming").iterdir()) == []


def test_flock_contenders_share_one_persistent_inode(transaction: Transaction) -> None:
    first, second = Lock(transaction.state), Lock(transaction.state)
    assert not first.held
    first.acquire()
    assert first.held
    inode = (transaction.state / "lock").stat().st_ino
    with pytest.raises(BusyError, match=r"^live deploy lock$"):
        second.acquire()
    assert not second.held
    with pytest.raises(RefusalError, match=r"^lock already held$"):
        first.acquire()
    first.release()
    first.release()
    assert not first.held
    second.acquire()
    assert second.held
    assert (transaction.state / "lock").stat().st_ino == inode
    second.release()
    assert stat.S_IMODE((transaction.state / "lock").stat().st_mode) == 0o600


def test_flock_released_when_owner_exits(transaction: Transaction) -> None:
    path = transaction.state / "lock"
    script = (
        "import fcntl,sys,time; f=open(sys.argv[1], 'a'); "
        "fcntl.flock(f, fcntl.LOCK_EX); print('ready', flush=True); time.sleep(30)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
        lock = Lock(transaction.state)
        with pytest.raises(BusyError, match=r"^live deploy lock$"):
            lock.acquire()
        child.kill()
        child.wait()
    lock.acquire()
    lock.release()


def test_flock_failure_closes_descriptor(transaction: Transaction) -> None:
    lock = Lock(transaction.state)
    with (
        patch.object(fcntl, "flock", side_effect=OSError("injected failure")),
        patch.object(os, "close", wraps=os.close) as close,
        pytest.raises(OSError, match="injected failure"),
    ):
        lock.acquire()
    assert not lock.held
    descriptor = close.call_args.args[0]
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def test_flock_never_follows_a_symlink(transaction: Transaction) -> None:
    path = transaction.state / "lock"
    target = transaction.state / "other"
    target.write_bytes(b"untouched")
    path.symlink_to(target)
    with pytest.raises(OSError, match=r"."):
        Lock(transaction.state).acquire()
    assert target.read_bytes() == b"untouched"


def test_backup_separates_payload_even_for_reserved_name(
    transaction: Transaction,
) -> None:
    secret = replace(transaction.service.secrets[0], name="metadata.json")
    transaction.service = replace(transaction.service, secrets=(secret,))
    fs.write(secret.target, b"payload")
    journal = transaction.save("a" * 40)
    saved = transaction.txn / "saved"
    assert (saved / "payload" / "metadata.json").read_bytes() == b"payload"
    assert (
        json.loads((saved / "metadata" / "metadata.json").read_bytes())[
            "metadata.json"
        ]["existed"]
        is True
    )
    transaction.write({"metadata.json": b"changed"})
    transaction.restore(journal)
    assert secret.target.read_bytes() == b"payload"
    transaction.finish(journal)
    transaction.write({"metadata.json": b"changed"})
    assert transaction.write_saved()
    assert secret.target.read_bytes() == b"payload"


@pytest.mark.parametrize(
    "failure", [CommandError, RefusalError, OSError, ValueError, KeyError, TypeError]
)
def test_recovery_restart_failure_remains_pending(
    transaction: Transaction, failure: type[Exception]
) -> None:
    transaction.pointer("current", "releases/" + "b" * 40)
    transaction.save("a" * 40)
    with (
        patch.object(transaction.engine, "up", side_effect=failure),
        pytest.raises(RefusalError, match=r"^recovery still pending$"),
    ):
        transaction.recover()
    assert transaction.journal() is not None


@pytest.mark.parametrize("field", ["release", "current", "previous"])
@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "releases/../outside",
        "releases/" + "a" * 39,
        "releases/" + "a" * 40 + "\n",
        "a" * 41,
    ],
)
def test_journal_validates_release_and_pointer_names(
    transaction: Transaction, field: str, name: str
) -> None:
    journal = transaction.save("a" * 40)
    journal[field] = name
    transaction.update(journal, "open")
    reason = "journal release name" if field == "release" else "pointer target"
    with pytest.raises(RefusalError, match="^" + reason + "$"):
        transaction.journal()


def test_prune_removes_release_digests(transaction: Transaction) -> None:
    seed_releases(transaction, tuple(range(7)))
    for number in range(7):
        release = f"{number:040x}"
        fs.write(transaction.service.root / "releases" / release / ".sha256", b"digest")
    transaction.prune()
    assert {path.name for path in (transaction.state / "models").iterdir()} == {
        f"{number:040x}.json" for number in range(2, 7)
    }


def test_lock_refuses_windows_before_opening_any_path(transaction: Transaction) -> None:
    with (
        patch.object(sys, "platform", "win32"),
        patch.object(fs, "parent") as parent,
        pytest.raises(OSError, match=r"^receive requires Unix$"),
    ):
        Lock(transaction.state).acquire()
    parent.assert_not_called()


def test_windows_import_does_not_load_unix_only_locking() -> None:
    script = """import builtins, sys
from tbhbops.deploy import engine
original = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == "fcntl":
        raise ImportError("Unix locking unavailable")
    return original(name, *args, **kwargs)
builtins.__import__ = without_fcntl
sys.platform = "win32"
from tbhbops.deploy import transaction
print("imported")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"imported\n"


def test_prune_only_unlinks_the_written_model_path(transaction: Transaction) -> None:
    seed_releases(transaction, tuple(range(6)))
    with patch.object(fs, "unlink", wraps=fs.unlink) as unlink:
        transaction.prune()
    assert unlink.call_args_list == [call(transaction.engine.model_path(f"{0:040x}"))]


def test_prune_ignores_stray_files_and_directory_links(
    transaction: Transaction,
) -> None:
    root = transaction.service.root
    releases = root / "releases"
    stray = releases / "stray"
    stray.write_bytes(b"unrelated")
    os.utime(stray, ns=(1, 1))
    linked = releases / "linked"
    linked.symlink_to(root / "data", target_is_directory=True)
    for number in range(7):
        (releases / str(number)).mkdir()
    transaction.prune()
    assert stray.read_bytes() == b"unrelated"
    assert linked.is_symlink()
    assert (root / "data" / "secrets").is_dir()
    assert len([path for path in releases.iterdir() if path.name.isdigit()]) == 5


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("second", [b"same", b"changed"])
def test_identical_retry_requires_all_secrets_and_no_pending_recreate(
    transaction: Transaction, force: bool, second: bytes
) -> None:
    first = transaction.service.secrets[0]
    fs.write(first.target, b"same", first.mode)
    first = replace(first, gid=first.target.stat().st_gid)
    other = replace(first, name="other", target=first.target.with_name("other"))
    fs.write(other.target, b"same", other.mode)
    transaction.service = replace(transaction.service, secrets=(first, other))
    release = "a" * 40
    transaction.pointer("current", "releases/" + release)
    if force:
        fs.write(transaction.state / "force-recreate", b"1\n")
    assert transaction.identical(release, {"token": b"same", "other": second}) is (
        not force and second == b"same"
    )
