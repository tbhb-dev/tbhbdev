import hashlib
import importlib.util
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
lazy from types import ModuleType

import pytest

SCRIPT = next(
    root / "apps/ca/backup/backup.py"
    for root in Path(__file__).resolve().parents
    if (root / "apps/ca/backup/backup.py").is_file()
)


@pytest.fixture
def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ca_backup", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def staged(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "ca"
    project.mkdir()
    for name in ("db", "config", "roots", "templates", "ts-state", "secrets"):
        directory = project / name
        directory.mkdir()
        (directory / "data").write_text(name, encoding="ascii")
    (project / "compose.yaml").write_text("name: ca\n", encoding="ascii")
    destination = tmp_path / "backups"
    destination.mkdir()
    key = tmp_path / "key"
    key.write_text(os.urandom(32).hex(), encoding="ascii")
    key.chmod(0o600)
    return project, destination, key


def allow_key(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests run as an unprivileged user; production requires root ownership.
    monkeypatch.setattr(module, "check_key", Mock())


def ca_healthy(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests have no Docker; production polls the container's health status.
    monkeypatch.setattr(module, "healthy", Mock(return_value=True))


def test_name_is_utc(module: ModuleType) -> None:
    date = datetime(2026, 11, 16, 4, 15, 0, 123456, tzinfo=timezone(timedelta(hours=1)))
    assert module.archive_name(date) == "ca-20261116T031500123456Z.tar.gz.enc"


def test_retention(module: ModuleType, tmp_path: Path) -> None:
    names = [f"ca-202611{day:02}T031500000000Z.tar.gz.enc" for day in range(10, 18)]
    for name in names:
        (tmp_path / name).touch()
        (tmp_path / (name + ".sha256")).touch()
    unrelated = tmp_path / "ca-unrelated.tar.gz.enc"
    unrelated.touch()
    module.retain(tmp_path)
    assert sorted(p.name for p in tmp_path.glob("*.enc")) == [
        *names[1:],
        unrelated.name,
    ]
    assert sorted(p.name for p in tmp_path.glob("*.sha256")) == [
        name + ".sha256" for name in names[1:]
    ]
    module.retain(tmp_path)
    assert len(list(tmp_path.iterdir())) == 15


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX OpenSSL")
def test_round_trip(
    module: ModuleType, staged: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, destination, key = staged
    original_run = module.run
    commands: list[list[str]] = []

    def execute(command: list[str], cwd: Path) -> None:
        commands.append(command)
        assert cwd == project
        if command[0] == "openssl":
            original_run(command, cwd)

    allow_key(module, monkeypatch)
    ca_healthy(module, monkeypatch)
    monkeypatch.setattr(module, "run", execute)
    target = module.backup(project, destination, key)
    assert commands[:2] == [
        ["/usr/local/bin/docker-compose", "-p", "ca", "stop", "step-ca"],
        ["/usr/local/bin/docker-compose", "-p", "ca", "start", "step-ca"],
    ]
    assert " ".join(commands[2][:6]) == ("openssl enc -aes-256-cbc -pbkdf2 -salt -in")
    assert target.name.startswith("ca-")
    assert target.name.endswith(".tar.gz.enc")
    assert target.with_suffix(".enc.sha256").read_text() == (
        f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {target.name}\n"
    )
    restored = project / "restored.tar.gz"
    subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-in",
            str(target),
            "-out",
            str(restored),
            "-pass",
            f"file:{key}",
        ],
        check=True,
        capture_output=True,
    )
    with tarfile.open(restored) as archive:
        names = archive.getnames()
        assert set(names) == {
            "compose.yaml",
            "db",
            "db/data",
            "config",
            "config/data",
            "roots",
            "roots/data",
            "templates",
            "templates/data",
        }
        content = archive.extractfile("db/data")
        assert content is not None
        assert content.read() == b"db"


@pytest.mark.parametrize(("uid", "mode"), [(1, 0o600), (0, 0o640)])
def test_reject_key(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, uid: int, mode: int
) -> None:
    monkeypatch.setattr(Path, "stat", Mock(return_value=Mock(st_uid=uid, st_mode=mode)))
    with pytest.raises(ValueError, match="owned by root"):
        module.check_key(Path("key"))


def test_missing_destination(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_key(module, monkeypatch)
    monkeypatch.setattr(Path, "is_dir", Mock(return_value=False))
    with pytest.raises(ValueError, match="existing directory"):
        module.backup(Path("ca"), Path("backups"), Path("key"))


@pytest.mark.parametrize(
    "failure",
    ["stop", "archive", "start", "openssl", "publish", "checksum", "retention"],
)
def test_failure_restarts_and_reports(
    module: ModuleType,
    staged: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    project, destination, key = staged
    allow_key(module, monkeypatch)
    ca_healthy(module, monkeypatch)
    monkeypatch.setattr(Path, "is_dir", Mock(return_value=True))
    commands: list[list[str]] = []

    def execute(command: list[str], _project: Path) -> None:
        commands.append(command)
        if failure in command:
            raise subprocess.CalledProcessError(1, command)

    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "injected failure"
        raise OSError(msg)

    monkeypatch.setattr(module, "run", execute)
    if failure == "archive":
        monkeypatch.setattr(module.tarfile, "open", fail)
    elif failure == "publish":
        monkeypatch.setattr(Path, "replace", fail)
    elif failure == "checksum":
        monkeypatch.setattr(Path, "write_text", fail)
    elif failure == "retention":
        monkeypatch.setattr(module, "retain", fail)
    # Mock encryption writes so later failure paths are reachable.
    monkeypatch.setattr(Path, "read_bytes", Mock(return_value=b"encrypted"))
    monkeypatch.setattr(Path, "write_bytes", Mock(return_value=9))
    # Isolate archive creation from publication failures.
    if failure != "archive":
        monkeypatch.setattr(
            module.tarfile,
            "open",
            Mock(
                return_value=Mock(
                    __enter__=Mock(return_value=Mock()),
                    __exit__=Mock(return_value=False),
                )
            ),
        )
    assert (
        module.main(
            [
                "--project",
                str(project),
                "--destination",
                str(destination),
                "--key-file",
                str(key),
            ]
        )
        == 1
    )
    assert "CA backup failed:" in capsys.readouterr().err
    assert commands[1][-2:] == ["start", "step-ca"]


START = ["/usr/local/bin/docker-compose", "-p", "ca", "start", "step-ca"]


def test_restart_waits_for_health(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock()
    sleep = Mock()
    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module.time, "sleep", sleep)
    monkeypatch.setattr(module, "healthy", Mock(side_effect=[False, False, True]))
    module.restart(START[:3], Path("ca"))
    run.assert_called_once_with(START, Path("ca"))
    assert sleep.call_args_list == [((module.HEALTH_INTERVAL,),)] * 2


def test_restart_retries_start_once(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(side_effect=[subprocess.CalledProcessError(1, START), None])
    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module, "healthy", Mock(return_value=True))
    module.restart(START[:3], Path("ca"))
    assert run.call_count == 2


def test_restart_retries_after_unhealthy_start(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock()
    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module.time, "sleep", Mock())
    states = [False] * module.HEALTH_POLLS + [True]
    monkeypatch.setattr(module, "healthy", Mock(side_effect=states))
    module.restart(START[:3], Path("ca"))
    assert run.call_count == 2


@pytest.mark.parametrize("start_fails", [True, False])
def test_restart_gives_up(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, *, start_fails: bool
) -> None:
    run = Mock()
    if start_fails:
        run.side_effect = subprocess.CalledProcessError(1, START)
    probe = Mock(return_value=False)
    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module.time, "sleep", Mock())
    monkeypatch.setattr(module, "healthy", probe)
    with pytest.raises(RuntimeError, match="not healthy after restart"):
        module.restart(START[:3], Path("ca"))
    assert run.call_count == module.START_ATTEMPTS
    assert probe.call_count == (
        0 if start_fails else module.START_ATTEMPTS * module.HEALTH_POLLS
    )


def test_unhealthy_ca_fails_backup(
    module: ModuleType,
    staged: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, destination, key = staged
    allow_key(module, monkeypatch)
    monkeypatch.setattr(module, "run", Mock())
    monkeypatch.setattr(module.time, "sleep", Mock())
    monkeypatch.setattr(module, "healthy", Mock(return_value=False))
    args = ["--project", str(project), "--destination", str(destination)]
    assert module.main([*args, "--key-file", str(key)]) == 1
    assert "not healthy after restart" in capsys.readouterr().err
    assert not list(destination.iterdir())


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("true healthy\n", True), ("true starting\n", False), ("false healthy\n", False)],
)
def test_healthy(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: bool,
) -> None:
    result = Mock(return_value=Mock(stdout=stdout))
    monkeypatch.setattr(module.subprocess, "run", result)
    assert module.healthy() is expected
    assert result.call_args.args[0] == [
        "/usr/local/bin/docker",
        "inspect",
        "--format",
        "{{.State.Running}} {{.State.Health.Status}}",
        "ca-fixture-ca-1",
    ]


def test_main_success(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    call = Mock()
    monkeypatch.setattr(module, "backup", call)
    assert module.main(["--destination", "/backup", "--key-file", "/key"]) == 0
    call.assert_called_once_with(
        Path("/srv/example-storage/docker/ca"), Path("/backup"), Path("/key")
    )


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX subprocess")
def test_cli_failure() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--destination",
            "/missing",
            "--key-file",
            "/missing/key",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "CA backup failed:" in result.stderr


def test_valid_key(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "stat", Mock(return_value=Mock(st_uid=0, st_mode=0o600)))
    module.check_key(Path("key"))
