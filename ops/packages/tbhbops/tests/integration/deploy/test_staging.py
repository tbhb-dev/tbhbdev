"""Verify release preparation, publication, and cleanup."""

import os
from dataclasses import replace
from unittest.mock import Mock, call, patch
lazy from pathlib import Path

import pytest
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy import receiver as receiver_module
from tbhbops.deploy import receiver_io, staging
from tbhbops.deploy.protocol import (
    RefusalError,
    Release,
    Request,
    pack,
)
from tbhbops.deploy.receiver import Receiver

from ...deploy_fakes import FakeCommand
from ...deploy_model import RELEASE
from ...deploy_receiver import PREVIOUS, engine, result, seed_previous


@pytest.mark.parametrize("extra", [0, 1])
def test_retained_bytes_boundary(receiver: Receiver, extra: int) -> None:
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


def test_release_conflict_and_policy_cleanup(receiver: Receiver) -> None:
    engine(receiver).fail_at = {"parse"}
    assert receiver.run() == 2
    assert result(receiver)["step"] == "policy"
    assert not (receiver.service.root / "releases" / RELEASE).exists()
    assert not receiver.service.secrets[0].target.exists()
    engine(receiver).fail_at.clear()
    receiver.staging.created = False
    assert receiver.run() == 0
    fs.write(receiver.service.root / "releases" / RELEASE / ".sha256", b"other")
    receiver.staging.created = False
    assert receiver.run() == 2
    assert result(receiver)["step"] == "release"


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
    receiver: Receiver, root_info: os.stat_result | None
) -> None:
    with (
        patch.object(fs, "inspect", return_value=root_info),
        patch.object(fs, "mkdir") as mkdir,
        pytest.raises(RefusalError, match=r"^service root ownership or mode$"),
    ):
        staging.prepare(receiver.service)
    mkdir.assert_not_called()


def test_stage_exact_files_and_modes(receiver: Receiver) -> None:
    staging.prepare(receiver.service)
    release = Release(
        {"release/config/nested/web": b"config", "release/compose.yaml": b"compose"},
        {"token": b"secret"},
        "abc123",
    )
    with patch.object(fs, "mkdir", wraps=fs.mkdir) as mkdir:
        receiver.staging.publish(receiver.service, receiver.wanted.release, release)
    assert receiver.staging.incoming is not None
    assert mkdir.call_args_list[0] == call(receiver.staging.incoming / "release", 0o755)
    directory = receiver.service.root / "releases" / RELEASE
    assert receiver.staging.created is True
    assert (directory / "config" / "nested" / "web").read_bytes() == b"config"
    assert (directory / "compose.yaml").read_bytes() == b"compose"
    assert (directory.stat().st_mode & 0o777) == 0o755
    assert ((directory / ".sha256").stat().st_mode & 0o777) == 0o644
    assert ((directory / "config").stat().st_mode & 0o777) == 0o755
    assert ((directory / "config" / "nested" / "web").stat().st_mode & 0o777) == 0o644
    assert (directory / ".sha256").read_bytes() == b"abc123"
    assert receiver.staging.incoming is not None
    assert receiver.staging.incoming.parent == receiver.service.root / "incoming"
    assert not (receiver.staging.incoming / "release").exists()
    assert not receiver.service.secrets[0].target.exists()
    receiver.staging.created = False
    receiver.staging.publish(receiver.service, receiver.wanted.release, release)
    assert receiver.staging.created is False
    with pytest.raises(RefusalError, match=r"^release digest conflict$"):
        receiver.staging.publish(
            receiver.service,
            receiver.wanted.release,
            replace(release, digest="different"),
        )


@pytest.mark.parametrize("free", [1024**3 - 1, 1024**3, 1024**3 + 1])
def test_free_space_boundary(receiver: Receiver, free: int) -> None:
    receiver.service = replace(receiver.service, min_free_gib=1)
    usage = Mock(free=free)
    with patch.object(receiver_module.shutil, "disk_usage", return_value=usage) as disk:
        assert receiver.run() == (2 if free < 1024**3 else 0)
    disk.assert_called_once_with(receiver.service.root)
    assert result(receiver)["step"] == ("space" if free < 1024**3 else "done")
    if free < 1024**3:
        assert result(receiver)["reason"] == "free space"


@pytest.mark.parametrize(
    "info", [None, os.stat_result((0o100700, 0, 0, 0, 1, 0, 0, 0, 0, 0))]
)
def test_prepare_init_owner(receiver: Receiver, info: os.stat_result | None) -> None:
    receiver.service = replace(receiver.service, init=("/root/init", "argument"))
    root_info = os.stat_result((0o040700, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    with (
        patch.object(fs, "inspect", side_effect=[root_info, info]) as inspect,
        patch.object(fs, "mkdir") as mkdir,
        pytest.raises(RefusalError, match=r"^init executable owner$"),
    ):
        staging.prepare(receiver.service)
    assert inspect.call_args_list == [
        call(receiver.service.root),
        call(Path("/root/init"), file=True),
    ]
    assert mkdir.call_args_list == [
        call(receiver.service.root / name)
        for name in ("deploy", "incoming", "releases")
    ] + [call(receiver.service.root / "deploy" / "models")]


def test_cleanup_removes_candidate_model_and_release_digest(
    receiver: Receiver,
) -> None:
    staging.prepare(receiver.service)
    receiver.staging.incoming = receiver.service.root / "incoming" / "staging"
    receiver.staging.incoming.mkdir()
    directory = receiver.service.root / "releases" / RELEASE
    directory.mkdir()
    model = receiver.engine.model_path(RELEASE)
    digest = directory / ".sha256"
    fs.write(model, b"{}")
    fs.write(digest, b"digest")
    receiver.staging.created = True
    receiver.cleanup(before_secrets=True)
    assert not receiver.staging.incoming.exists()
    assert not directory.exists()
    assert not model.exists()
    assert not digest.exists()


@pytest.mark.parametrize("after_rename", [False, True])
def test_digest_precedes_release_publish(
    receiver: Receiver, after_rename: bool
) -> None:
    staging.prepare(receiver.service)
    release = Release({"release/config": b"config"}, {}, "digest")
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
        receiver.staging.publish(receiver.service, receiver.wanted.release, release)
    assert destination.exists() is after_rename
    if after_rename:
        assert (destination / ".sha256").read_bytes() == b"digest"
    receiver.staging.publish(receiver.service, receiver.wanted.release, release)
    assert (destination / ".sha256").read_bytes() == b"digest"


def test_legacy_published_release_without_digest_refuses(
    receiver: Receiver,
) -> None:
    staging.prepare(receiver.service)
    destination = receiver.service.root / "releases" / RELEASE
    destination.mkdir()
    release = Release({}, {}, "digest")
    for _ in range(2):
        with pytest.raises(RefusalError, match=r"^published release missing digest$"):
            receiver.staging.publish(receiver.service, receiver.wanted.release, release)
    assert destination.is_dir()
    assert not (destination / ".sha256").exists()


def test_existing_digest_requires_regular_control_file(
    receiver: Receiver,
) -> None:
    staging.prepare(receiver.service)
    destination = receiver.service.root / "releases" / RELEASE
    destination.mkdir()
    (destination / ".sha256").mkdir()
    with pytest.raises(RefusalError, match=r"^path type$"):
        receiver.staging.publish(
            receiver.service, receiver.wanted.release, Release({}, {}, "digest")
        )


@pytest.mark.parametrize("writable", [False, True])
def test_prepare_walks_init_ancestors(receiver: Receiver, writable: bool) -> None:
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
                staging.prepare(receiver.service)
        else:
            staging.prepare(receiver.service)


@pytest.mark.parametrize("failure", ["pull", "init"])
def test_repeated_padded_failures_prune_before_space_and_remove_release(
    receiver: Receiver, failure: str
) -> None:
    staging.prepare(receiver.service)
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
        patch.object(receiver_module.shutil, "disk_usage", side_effect=space),
        patch.object(staging, "prepare"),
    ):
        for number in range(10, 18):
            release = f"{number:040x}"
            receiver.wanted = Request("o11y", release, None)
            receiver.staging.created = False
            body = pack(
                receiver.wanted, {"release/padding": b"x" * 4096}, {"token": b"fixture"}
            )
            engine(receiver).fail_at = {"pull " + release}
            fake = receiver.engine.command
            assert isinstance(fake, FakeCommand)
            fake.fail_argv = list(receiver.service.init) if failure == "init" else []
            with patch.object(receiver_io, "stream", return_value=body):
                assert receiver.run() == 4
            assert result(receiver)["outcome"] == "aborted"
            assert len(list(releases.iterdir())) <= 5
            assert not (releases / release).exists()
            assert not receiver.engine.model_path(release).exists()
