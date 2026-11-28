"""Exercise coordinator metadata through fake tools and run records."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from typing import TYPE_CHECKING

import pytest
from driver_scenarios import (
    COMMIT,
    CONFIG,
    arguments,
    command,
    launch,
)

from tbhbdev.alpha.agent.driver import arguments as agent_arguments
from tbhbdev.alpha.agent.driver import launch as coordinator
from tbhbdev.alpha.agent.driver import (
    metadata,
)
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

    from agent_driver_example import Remote


@pytest.mark.usefixtures("isolated_home")
def test_the_launch_lock_excludes_a_second_holder() -> None:
    with metadata.launch_lock():
        descriptor = os.open(metadata.directory() / "launch.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)


def test_metadata_round_trip_and_exclusive_create(remote: Remote) -> None:
    options = ("--account", "ops", "--unsandboxed", "o/r#1")
    assert launch(remote, *options) == 0
    run = metadata.load(CONFIG, "sample")
    assert run.host.account == "ops"
    assert run.request.account == "ops"
    assert run.request.unsandboxed
    assert run.request.authorized_by == "o/r#1"
    with pytest.raises(FileExistsError):
        metadata.save(run)


@pytest.mark.parametrize("older_record", [False, True])
def test_cli_identity_round_trip(remote: Remote, older_record: bool) -> None:
    args = agent_arguments.parser("example agent").parse_args(arguments(remote))
    run = coordinator.prepare(CONFIG, args)
    run.cli_commit = COMMIT
    run.cli_sha256 = "a" * 64
    metadata.save(run)
    path = metadata.metadata_path("sample")
    record = json.loads(path.read_text())
    assert (record["cli_commit"], record["cli_sha256"]) == (COMMIT, "a" * 64)
    if older_record:
        del record["cli_commit"]
        del record["cli_sha256"]
        path.write_text(json.dumps(record))
    loaded = metadata.load(CONFIG, "sample")
    expected = (None, None) if older_record else (COMMIT, "a" * 64)
    assert (loaded.cli_commit, loaded.cli_sha256) == expected


@pytest.mark.parametrize("xdg", [None, "", "relative/state"])
def test_metadata_name_and_default_state(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, xdg: str | None
) -> None:

    with pytest.raises(Invalid, match="run name"):
        metadata.metadata_path("../bad")
    if xdg is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", xdg)
    monkeypatch.setenv("HOME", str(remote.root))
    assert metadata.directory() == remote.root / ".local/state/tbhbdev/agent-runs"


def test_metadata_permissions_under_an_open_mask(remote: Remote) -> None:
    args = agent_arguments.parser("example agent").parse_args(arguments(remote))
    run = coordinator.prepare(CONFIG, args)
    previous = os.umask(0)
    try:
        metadata.save(run)
    finally:
        os.umask(previous)
    path = metadata.metadata_path("sample")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    loaded = metadata.load(CONFIG, "sample")
    assert loaded.started == run.started
    assert loaded.request == run.request
    assert loaded.host == run.host
    assert loaded.name == run.name


@pytest.mark.parametrize(
    "record", ["{", "[]", "{}", '{"host":"example-linux"}', "0", "null"]
)
def test_malformed_metadata_names_file(
    remote: Remote, capsys: pytest.CaptureFixture[str], record: str
) -> None:
    path = metadata.metadata_path("sample")
    assert path.is_relative_to(remote.local)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record)
    assert command("status") == 1
    assert str(path) in capsys.readouterr().err


def test_existing_metadata_directory_permissions(remote: Remote) -> None:
    path = metadata.metadata_path("sample")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    assert launch(remote) == 0
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_load_hardens_existing_metadata_directory(remote: Remote) -> None:
    assert launch(remote) == 0
    path = metadata.metadata_path("sample")
    path.parent.chmod(0o755)
    assert command("status") == 0
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_metadata_directory_is_private_before_chmod(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = agent_arguments.parser("example agent").parse_args(arguments(remote))
    run = coordinator.prepare(CONFIG, args)
    directory = metadata.metadata_path("sample").parent
    original = type(directory).chmod

    def chmod(path: Path, mode: int) -> None:
        assert path == directory
        assert path.stat().st_mode & 0o777 == 0o700
        original(path, mode)

    monkeypatch.setattr(type(directory), "chmod", chmod)
    previous = os.umask(0)
    try:
        metadata.save(run)
    finally:
        os.umask(previous)


def test_not_owner_archives_with_utc_stamp(remote: Remote) -> None:
    assert launch(remote) == 0
    remote.file(".owner", "new-owner\n")
    assert command("status") == 1
    archived = next((metadata.directory() / "trash").glob("*/sample.json"))
    stamp = dt.datetime.fromisoformat(archived.parent.name)
    assert stamp.utcoffset() == dt.timedelta(0)
