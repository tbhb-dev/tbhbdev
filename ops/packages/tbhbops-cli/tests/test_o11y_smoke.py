"""Smoke runs against an in-memory host and a scripted local engine."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

import argparse
import errno
import io
import json
import os
import signal
import stat
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Self

import pytest
from tbhbops.o11y import init, smoke
from tbhbops_cli import cli
from tbhbops_cli.o11y import (
    polling,
    smoke_capacity,
    smoke_checks,
    smoke_lifecycle,
    smoke_stages,
)
from tbhbops_cli.o11y import smoke as command
from tbhbops_cli.o11y.ports import LocalPorts
from tbhbops_cli.o11y.runner import Result
lazy from tbhbops.o11y.validate import Document
lazy from tbhbops_cli.o11y.filesystem import Filesystem

from .test_o11y_cli import SOURCE, Fake, render_fixture, world  # pyrefly: ignore[missing-import]  # pytest importlib supplies this namespace

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

OUT = Path("/output")
ARTIFACTS = Path("/artifacts")
HOST = Path("/temporary")


@pytest.fixture(autouse=True)
def slice_a_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    def skip(_run: command.Smoke) -> None:
        pass

    for stage in ("group4", "group5", "export_persistence", "group6"):
        monkeypatch.setattr(smoke_stages, stage, skip)

    def skip_later(*_args: object) -> None:
        pass

    for module, names in (
        (smoke_capacity, ("smoke", "cleanup")),
        (smoke_checks, ("group7", "group11", "group12")),
        (smoke_lifecycle, ("group8", "group9", "group10", "group13")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, skip_later)


def interrupt(number: int = signal.SIGINT) -> NoReturn:
    handler = signal.getsignal(number)
    assert callable(handler)
    handler(number, None)
    msg = "installed signal handler returned"
    raise AssertionError(msg)


def bounded(result: Result, byte_limit: int, line_limit: int) -> Result:
    raw = result.stdout.encode(errors="surrogateescape")[:byte_limit]
    return replace(
        result,
        stdout=b"".join(io.BytesIO(raw).readlines()[:line_limit]).decode(
            errors="surrogateescape"
        ),
    )


@dataclass
class Ports:
    bound: int | None = None
    calls: list[int] = field(default_factory=list)

    def available(self, port: int) -> bool:
        self.calls.append(port)
        return port != self.bound


def model() -> Document:
    return {
        "services": {
            "rustfs": {"image": "rustfs:pinned"},
            **{
                name: {
                    "depends_on": {
                        "s3-buckets": {"condition": "service_completed_successfully"}
                    }
                }
                for name in ("loki", "tempo", "mimir")
            },
            "s3-buckets": {"depends_on": {"rustfs": {"condition": "service_healthy"}}},
            "mimir-health-tools": {"image": "busybox:pinned"},
        }
    }


@dataclass
class Engine:
    fs: Fake
    calls: list[tuple[list[str], dict[str, str], int]] = field(default_factory=list)
    fail_at: int = -1
    replacement: Result = field(
        default_factory=lambda: Result(17, "untrusted body", "untrusted error")
    )
    head_calls: list[tuple[list[str], int, int, int]] = field(default_factory=list)
    interrupt_at: int = -1
    bootstraps: int = 0
    fail_match: str = ""
    contaminated: str = ""
    disk: Result = field(default_factory=lambda: Result(0, "1000 200"))
    log_error: str = ""
    identity: str = "native|Linux"

    def read_head(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        byte_limit: int,
        line_limit: int,
    ) -> Result:
        self.head_calls.append((list(argv), timeout, byte_limit, line_limit))
        assert byte_limit == 4 * 1024 * 1024
        assert line_limit == 200
        return bounded(self.run(argv, env, timeout), byte_limit, line_limit)

    def copy(self, source: Path, target: Path) -> None:
        self.fs.mkdir(target)
        for path in list(self.fs.modes):
            if source in path.parents:
                destination = target / path.relative_to(source)
                self.fs.modes[destination] = self.fs.modes[path]
                if path in self.fs.files:
                    self.fs.files[destination] = self.fs.files[path]
                if path in self.fs.owners:
                    self.fs.owners[destination] = self.fs.owners[path]

    def path(self, raw: str, argv: list[str]) -> Path:
        for index, arg in enumerate(argv):
            if arg == "-v":
                source, destination, *_ = argv[index + 1].split(":")
                if raw == destination or raw.startswith(destination + "/"):
                    return Path(source) / raw.removeprefix(destination).lstrip("/")
        raise AssertionError(raw)

    def utility(self, argv: list[str]) -> Result:  # fake utility dispatch
        index = argv.index("--entrypoint")
        executable, args = argv[index + 1], argv[index + 3 :]
        if executable == "test":
            path = self.path(args[1], argv)
            info = self.fs.lstat(path)
            return Result(0 if info is not None and stat.S_ISLNK(info.st_mode) else 1)
        if executable == "stat":
            if args[0] == "-f":
                return self.disk
            path = self.path(args[-1], argv)
            info = self.fs.lstat(path)
            assert info is not None
            uid, gid = self.fs.owners[path]
            return Result(0, f"{uid} {gid} {info.st_mode & 0o777:o} {info.st_size}")
        return self.mutate(executable, args, argv)

    def mutate(self, executable: str, args: list[str], argv: list[str]) -> Result:
        if executable == "cp":
            source, target = (self.path(raw, argv) for raw in args[-2:])
            if source.name == ".rustfs.sys":
                self.fs.mkdir(target / source.name)
            else:
                self.copy(source, target)
        elif executable == "chown":
            self.chown(args, argv)
        elif executable == "chmod":
            self.chmod(args, argv)
        elif executable == "rm":
            assert args[0] == "-rf"
            for raw in args[1:]:
                command.render.remove_tree(self.fs, self.path(raw, argv))
        else:
            raise AssertionError(executable)
        return Result(0)

    def chown(self, args: list[str], argv: list[str]) -> None:
        owner = args[1] if args[0] == "-R" else args[0]
        uid, gid = map(int, owner.split(":"))
        names = args[2:] if args[0] == "-R" else args[1:]
        for raw in names:
            self.fs.chown(self.path(raw, argv), uid, gid)

    def chmod(self, args: list[str], argv: list[str]) -> None:
        if args[0] == "-R":
            target = self.path(args[-1], argv)
            for path in self.fs.modes:
                if path == target or target in path.parents:
                    mode = 0o755 if stat.S_ISDIR(self.fs.modes[path]) else 0o644
                    self.fs.chmod(path, mode)
        else:
            self.fs.chmod(self.path(args[-1], argv), int(args[0], 8))

    def run(  # fake Docker API routing
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        assert input_data is None
        argv = list(argv)
        self.calls.append((argv, dict(env), timeout))
        if len(self.calls) == self.interrupt_at:
            interrupt()
        if len(self.calls) == self.fail_at or (
            self.fail_match and self.fail_match in " ".join(argv)
        ):
            return self.replacement
        if argv[1:3] == ["context", "show"]:
            return Result(0, "default")
        if argv[1:3] == ["context", "inspect"]:
            return Result(0, "unix:///var/run/docker.sock")
        if argv[1] == "info":
            return Result(0, self.identity)
        if argv[1] == "version":
            return Result(0, "1.44")
        if argv[1] == "inspect":
            return self.inspect(argv)
        if argv[1:3] == ["rm", "-f"]:
            return Result(0)
        if argv[1] == "run":
            return self.utility(argv)
        return self.compose(argv, env)

    @staticmethod
    def inspect(argv: list[str]) -> Result:
        name = argv[-1]
        template = argv[-2]
        if template == "{{.State.Health.Status}}":
            return Result(
                0, "unhealthy" if name == "readiness-unhealthy" else "healthy"
            )
        if template == "{{.Id}} {{.State.StartedAt}}":
            return Result(0, name + " 2026-10-25T05:38:26Z")
        return Result(0, "exited:42" if name == "readiness-failed" else "exited 0")

    def compose(
        self, argv: list[str], env: Mapping[str, str]
    ) -> Result:  # fake compose routes
        assert argv[1] == "compose"
        if argv[-2:] == ["--format", "json"]:
            return Result(0, json.dumps(model()))
        if "ps" in argv:
            return Result(0, argv[-1])
        if "exec" in argv:
            return Result(
                0,
                "ingester:\n  wal:\n    disk_full_threshold: 0.99\n200"
                if argv[-1].endswith("/config")
                else "200",
            )
        if "up" in argv:
            return self.up(argv, env)
        if "logs" in argv:
            return self.logs(argv)
        if "run" in argv and "s3-buckets" in argv:
            self.bootstraps += 1
            return self.buckets(() if self.bootstraps == 1 else ("mimir-ruler",))
        if "s3probe" in argv and argv[-1] == "buckets":
            return Result(0, "\n".join(name + " timestamp" for name in smoke.BUCKETS))
        return Result(0, "done")

    def up(self, argv: list[str], env: Mapping[str, str]) -> Result:
        data = Path(env["O11Y_DATA_DIR"])
        self.fs.mkdir(data / "rustfs/.rustfs.sys")
        if str(OUT / "readiness-fixture.yaml") in argv:
            fixture = json.loads(self.fs.read_text(OUT / "readiness-fixture.yaml"))
            project = argv[argv.index("-p") + 1]
            self.check_fixture(fixture, project)
            return Result(1, "fixture refused")
        return Result(0, "started")

    def logs(self, argv: list[str]) -> Result:
        service = argv[-1]
        if service == "rustfs":
            return Result(0, '{"console_enabled":false}')
        if service == "s3-buckets":
            return self.buckets(smoke.BUCKETS)
        assert argv[argv.index("logs") :] == (
            ["logs", "--no-color", "--tail", "200", service]
            if "--tail" in argv
            else ["logs", "--no-color", "loki"]
        )
        assert service in ("alloy", "loki")
        data = "".join(f"{i}\n" for i in range(1, 5001))
        if self.contaminated == ("loki-head" if "--tail" not in argv else service):
            data += self.fs.read_text(HOST / "secrets/rustfs.secret_key")
        return Result(0, data, self.log_error)

    @staticmethod
    def check_fixture(fixture: Document, project: str) -> None:
        if "readiness-unhealthy" in fixture["services"]:
            assert fixture == {
                "services": {
                    "readiness-unhealthy": {
                        "image": "busybox:pinned",
                        "container_name": project + "-readiness-unhealthy",
                        "restart": "unless-stopped",
                        "command": ["sleep", "300"],
                        "healthcheck": {
                            "test": ["CMD", "false"],
                            "interval": "1s",
                            "timeout": "1s",
                            "retries": 1,
                        },
                    }
                }
            }
        else:
            assert fixture == {
                "services": {
                    "readiness-failed": {
                        "image": "busybox:pinned",
                        "container_name": project + "-readiness-failed",
                        "restart": "no",
                        "command": ["sh", "-c", "exit 42"],
                    },
                    "readiness-consumer": {
                        "image": "busybox:pinned",
                        "container_name": project + "-readiness-consumer",
                        "restart": "no",
                        "command": ["true"],
                        "depends_on": {
                            "readiness-failed": {
                                "condition": "service_completed_successfully"
                            }
                        },
                    },
                }
            }

    @staticmethod
    def buckets(created: tuple[str, ...]) -> Result:
        return Result(
            0,
            "\n".join(
                f"bucket {name} {'created' if name in created else 'exists'}"
                for name in smoke.BUCKETS
            ),
        )


def setup() -> tuple[Fake, Engine, cli.System]:
    fs = Fake()
    original_check = fs.check

    def check(operation: str, path: Path) -> None:
        assert isinstance(path, Path)
        original_check(operation, path)

    fs.check = check
    render_fixture(fs)
    fs.modes[Path("/var/run/docker.sock")] = stat.S_IFSOCK | 0o600
    engine = Engine(fs)
    draws = iter(enumerate((4, 20, 40, 32, 32, 16, 16), 1))

    def random_bytes(size: int) -> bytes:
        number, expected = next(draws)
        assert size == expected
        return bytes([number]) * size

    elapsed = 0.0

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        assert seconds == 2
        assert elapsed <= 400
        elapsed += seconds

    time_reads = 0

    def monotonic() -> float:
        nonlocal time_reads
        time_reads += 1
        assert time_reads <= 1000
        return elapsed

    system = replace(
        world(fs, 123),
        egid=456,
        cwd=Path("/"),
        env={
            "HOME": "/operator",
            "PATH": "/bin",
            "DOCKER_CONTEXT": "remote",
            "DOCKER_HOST": "secret-value",
            "O11Y_DATA_DIR": "secret-value",
            "COMPOSE_FILE": "secret-value",
            "AWS_SECRET_ACCESS_KEY": "secret-value",
        },
        runner=engine,
        ports=Ports(),
        platform="linux",
        engine_hostname="native",
        random_bytes=random_bytes,
        monotonic=monotonic,
        sleep=sleep,
    )
    return fs, engine, system


def run(system: cli.System) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(
        [
            "o11y",
            "smoke",
            "--source",
            str(SOURCE),
            "--out",
            str(OUT),
            "--artifacts",
            str(ARTIFACTS),
        ],
        system,
        out,
        err,
    )
    assert err.getvalue() == ""
    return code, out.getvalue()


def test_success_and_environment() -> None:
    fs, engine, system = setup()
    code, output = run(system)
    assert code == 0, output
    assert (
        output == "engine mode: local\nsmoke setup\n"
        f"ok 1: {smoke.SUCCESS['1']}\n"
        "smoke Loki disk usage (started): 0.800000\n"
        f"ok 2: {smoke.SUCCESS['2']}\n"
        f"ok 3: {smoke.SUCCESS['3']}\n"
        "smoke Loki WAL threshold: 0.99\n"
        f"ok 3a: {smoke.SUCCESS['3a']}\n"
    )
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
    assert fs.list_dir(ARTIFACTS) == ()
    assert "secret-value" not in output
    assert isinstance(system.ports, Ports)
    assert system.ports.calls == [13000, 14317, 14318]
    for argv, env, timeout in engine.calls:
        if "--wait" in argv:
            assert timeout == (60 if argv[-1] == "30" else 210)
        elif "up" in argv:
            assert timeout == 600
        assert "secret-value" not in str(argv)
        assert "DOCKER_CONTEXT" not in env
        assert "DOCKER_HOST" not in env
        assert not any(key.startswith("COMPOSE_") for key in env)
        assert {key: env[key] for key in smoke.FIXED_ENV} == smoke.FIXED_ENV
        assert env["O11Y_DATA_DIR"] == str(HOST)
        assert env["O11Y_SECRETS_OWNER"] == "123:456"
        if argv[1] == "compose":
            assert argv[argv.index("-p") + 1] == "o11y-smoke-01010101"
        if "exec" in argv:
            assert argv[argv.index("exec") : argv.index("curl") + 1] == [
                "exec",
                "-T",
                "rustfs",
                "curl",
            ]
            assert argv[argv.index("-m") + 1] == "10"
            assert timeout == 15
            assert timeout > int(argv[argv.index("-m") + 1])
        if "--entrypoint" in argv:
            assert argv[argv.index("--network") + 1] == "none"
            assert argv[argv.index("--log-driver") + 1] == "none"
            assert argv[argv.index("--entrypoint") + 1] not in ("sh", "/bin/sh")
    assert engine.calls[-1][0][-10:] == [
        "rustfs:pinned",
        "-rf",
        "/d/alloy",
        "/d/config",
        "/d/grafana",
        "/d/loki",
        "/d/mimir",
        "/d/rustfs",
        "/d/secrets",
        "/d/tempo",
    ]


def test_environment_builder_wal_override() -> None:
    env = command.environment(
        {
            "PATH": "p",
            "HOME": "h",
            "TMPDIR": "t",
            "DOCKER_CONFIG": "c",
            "DOCKER_CONTEXT": "remote",
            "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD": "0.1",
            "COMPOSE_PROFILES": "bad",
        },
        HOST,
        (1, 2),
    )
    assert env == {
        **smoke.FIXED_ENV,
        "PATH": "p",
        "HOME": "h",
        "TMPDIR": "t",
        "DOCKER_CONFIG": "c",
        "O11Y_DATA_DIR": str(HOST),
        "O11Y_SECRETS_OWNER": "1:2",
    }
    assert (
        command.environment({}, HOST, (1, 2))["O11Y_LOKI_WAL_DISK_FULL_THRESHOLD"]
        == "0.99"
    )


@pytest.mark.parametrize("port", smoke.PORTS)
def test_bound_port_before_up(port: int) -> None:
    fs, engine, system = setup()
    code, output = run(replace(system, ports=Ports(port)))
    assert code == 2
    assert "usage: loopback port already bound" in output
    assert not any("up" in argv for argv, _, _ in engine.calls)
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None


@pytest.mark.parametrize("path", [OUT, ARTIFACTS, HOST, OUT.parent])
def test_symlink_paths_refused(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, system = setup()
    fs.modes[path] = stat.S_IFLNK
    monkeypatch.setattr(fs, "temporary_directory", lambda: HOST)
    code, output = run(system)
    assert code == 2
    assert "symlink path refused" in output
    assert not any(
        "up" in argv or "cp" in argv or "rm" in argv for argv, _, _ in engine.calls
    )


def test_interrupt_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, system = setup()
    engine.fail_match = "up -d"
    original = engine.run

    def interrupted(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        if "up" in argv:
            interrupt()
        return result

    monkeypatch.setattr(engine, "run", interrupted)
    code, output = run(system)
    assert code == 130
    assert "FAIL interrupted" in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
    interrupted_up = next(
        i for i, (argv, _, _) in enumerate(engine.calls) if "up" in argv
    )
    assert any("down" in argv for argv, _, _ in engine.calls[interrupted_up + 1 :])


def test_failure_capture_clean_bounded() -> None:
    fs, engine, system = setup()
    engine.fail_match = "up -d"
    code, output = run(system)
    assert code == 1
    assert "FAIL 2: up failed" in output
    assert "untrusted" not in output
    assert "clean Alloy and Loki logs retained" in output
    assert [
        (argv[-3:], timeout, byte_limit, line_limit)
        for argv, timeout, byte_limit, line_limit in engine.head_calls
    ] == [(["logs", "--no-color", "loki"], 120, 4 * 1024 * 1024, 200)]
    assert fs.list_dir(ARTIFACTS) == ("alloy.log", "loki-head.log", "loki.log")
    for name in fs.list_dir(ARTIFACTS):
        content = fs.read_text(ARTIFACTS / name)
        assert len(content.splitlines()) == 200
        assert fs.modes[ARTIFACTS / name] & 0o777 == 0o600
    assert fs.modes[ARTIFACTS] & 0o777 == 0o700
    assert fs.read_text(ARTIFACTS / "loki-head.log") == "".join(
        f"{n}\n" for n in range(1, 201)
    )
    assert fs.read_text(ARTIFACTS / "loki.log") == "".join(
        f"{n}\n" for n in range(4801, 5001)
    )
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
    assert ("mkdir", OUT / "failure-logs") in fs.calls
    assert ("chmod", OUT / "failure-logs", 0o700) in fs.calls
    for name in ("alloy", "loki", "loki-head"):
        assert ("write", OUT / "failure-logs" / (name + ".log"), 0o600) in fs.calls


@pytest.mark.parametrize("name", ["loki", "loki-head"])
def test_contaminated_capture_withheld(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, system = setup()
    engine.fail_match = "up -d"
    engine.contaminated = name
    # Head contamination must be inside the first 200 lines.
    original = engine.run

    def contaminate(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        head = "logs" in argv and argv[-1] == "loki" and "--tail" not in argv
        if name == "loki-head" and head:
            return Result(
                0, fs.read_text(HOST / "secrets/rustfs.secret_key") + result.stdout
            )
        return result

    monkeypatch.setattr(engine, "run", contaminate)
    code, output = run(system)
    assert code == 1
    assert "no logs promoted" in output
    assert fs.list_dir(ARTIFACTS) == ()
    assert "C" * 40 not in output
    assert fs.lstat(OUT) is None


@pytest.mark.parametrize(
    "fail", ["logs --no-color --tail 200 loki", "logs --no-color loki"]
)
def test_failed_capture_promotes_nothing(
    fail: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, system = setup()
    original = engine.run

    def failure(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        return (
            Result(17, "untrusted", "untrusted")
            if fail in " ".join(argv) or "up" in argv
            else result
        )

    monkeypatch.setattr(engine, "run", failure)
    code, output = run(system)
    assert code == 1
    assert "no logs promoted" in output
    assert "untrusted" not in output
    assert fs.list_dir(ARTIFACTS) == ()
    assert fs.lstat(OUT) is None


@pytest.mark.parametrize("exc", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("name", ["alloy.log", "loki-head.log"])
def test_failed_or_interrupted_scan_preserves_status(
    exc: type[BaseException], name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, system = setup()
    engine.fail_match = "up -d"
    original = command.scan_path

    def failure(filesystem: Filesystem, path: Path, needles: tuple[bytes, ...]) -> None:
        if path.name == name:
            if exc is KeyboardInterrupt:
                interrupt()
            raise exc
        original(filesystem, path, needles)

    monkeypatch.setattr(command, "scan_path", failure)
    code, output = run(system)
    assert code == 1
    assert "no logs promoted" in output
    assert fs.lstat(OUT) is None
    assert fs.list_dir(ARTIFACTS) == ()


def test_early_exit_no_prerequisites() -> None:
    fs, engine, system = setup()
    engine.fail_match = "config -q"
    code, output = run(system)
    assert code == 1
    assert "capture unavailable (stack not started)" in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
    assert not any("down" in argv for argv, _, _ in engine.calls)


@pytest.mark.parametrize("failure", ["transcript", "cleanup"])
def test_finalization_failure_preserves_original(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, system = setup()
    engine.fail_match = "up -d"
    if failure == "transcript":
        fs.denied_calls.add(("open", OUT / "transcript.log"))
        original = fs.write_atomic

        def write(path: Path, text: str | bytes, mode: int) -> None:
            if path.name == "transcript.log":
                raise OSError
            original(path, text, mode)

        monkeypatch.setattr(fs, "write_atomic", write)
    else:

        def cleanup(_self: command.Smoke) -> bool:
            return False

        monkeypatch.setattr(command.Smoke, "cleanup", cleanup)
    code, output = run(system)
    assert code == 1
    assert (
        "transcript finalization failed"
        if failure == "transcript"
        else "cleanup failed"
    ) in output


def test_diagnostics_disk_unavailable_and_pressure() -> None:
    for disk, suffix in (
        (Result(14, "private", "private"), "unavailable"),
        (
            Result(0, "1000 50"),
            "ratio=0.950000 at or above 0.90 (pressure at collection time)",
        ),
    ):
        _, engine, system = setup()
        engine.fail_match = "up -d"
        engine.disk = disk
        code, output = run(system)
        assert code == 1
        assert "smoke diagnostics: Loki disk usage " + suffix in output
        assert "inconclusive (no transition in head and tail windows)" in output
        assert "private" not in output


@pytest.mark.parametrize("phrase", [smoke.THROTTLE, smoke.RESUME])
def test_diagnostics_transitions(phrase: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, engine, system = setup()
    engine.fail_match = "up -d"
    original = engine.run

    def transition(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        return (
            Result(0, phrase + ' label="private"\n' + result.stdout)
            if "logs" in argv and argv[-1] == "loki"
            else result
        )

    monkeypatch.setattr(engine, "run", transition)
    code, output = run(system)
    assert code == 1
    assert phrase in output
    assert 'label="private"' not in output
    assert ("throttling confirmed" in output) == (phrase == smoke.THROTTLE)


def test_scan_names_only_and_binary() -> None:
    fs = Fake()
    fs.write_atomic(Path("/candidate"), b"\xff\x00TOKEN\xfe", 0o600)
    with pytest.raises(command.SmokeError, match=r"^sentinel secret: candidate$"):
        command.scan_path(fs, Path("/candidate"), (b"TOKEN",))
    fs.write_atomic(Path("/candidate"), b"\xff\x00clean\xfe", 0o600)
    command.scan_path(fs, Path("/candidate"), (b"TOKEN",))


def test_poll_timing() -> None:
    _, _, system = setup()
    calls = []

    def check() -> bool:
        calls.append(system.monotonic())
        return False

    assert not polling.poll(system, 30, check)
    assert calls == list(range(0, 31, 2))
    assert polling.poll(system, 180, lambda: True)
    assert (
        command.READY_TIMEOUT,
        command.HEALTH_TIMEOUT,
        command.FIXTURE_TIMEOUT,
        polling.POLL_INTERVAL,
        command.WAIT_MARGIN,
        command.PULL_TIMEOUT,
    ) == (180, 120, 30, 2, 30, 600)


def test_signal_handler_and_restore() -> None:
    previous = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    with pytest.raises(command.SmokeError) as failure, command.signals():
        interrupt(signal.SIGTERM)
    assert failure.value.status == 143
    assert str(failure.value) == "interrupted"
    assert {n: signal.getsignal(n) for n in previous} == previous


def test_no_leak_flag() -> None:
    for flag in ("--leak",):
        with pytest.raises(SystemExit) as failure:
            cli.parser().parse_args(
                [
                    "o11y",
                    "smoke",
                    "--source",
                    "/src",
                    "--out",
                    "/out",
                    "--artifacts",
                    "/artifacts",
                    flag,
                    "5",
                ]
            )
        assert failure.value.code == 2


def test_ports_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        def __init__(self) -> None:
            self.bound: list[tuple[str, int]] = []
            self.fail = False

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            self.bound.append(address)
            if self.fail:
                raise OSError

    socket = Socket()

    def socket_factory(_family: int, _kind: int) -> Socket:
        assert (_family, _kind) == (2, 1)
        return socket

    monkeypatch.setattr("tbhbops_cli.o11y.ports.socket.socket", socket_factory)
    assert LocalPorts().available(13000)
    socket.fail = True
    assert not LocalPorts().available(14317)
    assert socket.bound == [("127.0.0.1", 13000), ("127.0.0.1", 14317)]


def prepared() -> tuple[Fake, Engine, command.Smoke]:
    fs, engine, system = setup()
    fs.mkdir(HOST)
    sentinels = smoke.sentinels(
        (
            *(bytes([n + 2]) * spec.length for n, spec in enumerate(init.SECRETS)),
            b"t" * 16,
            b"s" * 16,
        )
    )
    instance = command.Smoke(
        system,
        OUT,
        ARTIFACTS,
        HOST,
        HOST,
        "o11y-smoke-01010101",
        sentinels,
        command.environment(system.env, HOST, (123, 456)),
    )
    instance.prepare(SOURCE)
    return fs, engine, instance


def test_macos_host_env_guard_and_cleanup() -> None:
    fs, engine, system = setup()
    engine.identity = "docker-desktop|Docker Desktop"
    code, output = run(replace(system, platform="darwin"))
    assert code == 0, output
    commands = [argv for argv, _, _ in engine.calls]
    copies = [argv for argv in commands if "cp" in argv]
    assert any(argv[-2:] == ["/host/.", "/d/smoke-01010101/"] for argv in copies)
    assert any(
        argv[-2:] == ["/d/smoke-01010101/rustfs/.rustfs.sys", "/host/rustfs/"]
        for argv in copies
    )
    assert any(
        argv[-2:] == ["/host/secrets/.", "/d/smoke-01010101/secrets/"]
        for argv in copies
    )
    for argv, env, _ in engine.calls:
        assert env["O11Y_DATA_DIR"] == "/var/lib/o11y-test/smoke-01010101"
        if "up" in argv:
            assert env["O11Y_S3_ENV_FILE"] == "/temporary/secrets/s3.env"
    assert fs.lstat(HOST) is None
    assert fs.lstat(Path("/var/lib/o11y-test/smoke-01010101")) is None
    assert fs.lstat(OUT) is None


@pytest.mark.parametrize(
    "path", [Path("/var/lib/o11y-test/smoke-01010101"), Path("/var/lib/o11y-test")]
)
def test_macos_symlink_data_refused(path: Path) -> None:
    fs, engine, system = setup()
    engine.identity = "docker-desktop|Docker Desktop"
    fs.modes[path] = stat.S_IFLNK
    code, output = run(replace(system, platform="darwin"))
    assert code == 1
    assert "data dir symlink or inspection failure" in output
    assert not any("cp" in argv for argv, _, _ in engine.calls)
    assert fs.modes[path] == stat.S_IFLNK
    assert fs.lstat(OUT) is None


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "empty-file",
        "empty-dir",
        "fifo",
        "nested-link",
        "nested-empty",
        "nested-binary",
    ],
)
def test_scan_fail_closed_tree(kind: str) -> None:
    fs = Fake()
    if kind == "empty-file":
        fs.write_atomic(OUT, "", 0o600)
    elif kind == "fifo":
        fs.modes[OUT] = stat.S_IFIFO
    elif kind != "missing":
        fs.mkdir(OUT)
        if kind.startswith("nested"):
            fs.mkdir(OUT / "sub")
            fs.write_atomic(OUT / "clean", "clean", 0o600)
            fs.write_atomic(OUT / "sub/empty", "", 0o600)
            if kind == "nested-link":
                fs.modes[OUT / "sub/link"] = stat.S_IFLNK
            elif kind == "nested-binary":
                fs.write_atomic(OUT / "sub/binary", b"\xffTOKEN\xfe", 0o600)
    if kind == "nested-empty":
        command.scan_path(fs, OUT, (b"TOKEN",))
    else:
        with pytest.raises(command.SmokeError):
            command.scan_path(fs, OUT, (b"TOKEN",))


def test_output_cap_and_inside_status() -> None:
    _, engine, instance = prepared()
    for code in (0, 17):
        engine.fail_match = "exec"
        engine.replacement = Result(code, "body\n200", "private")
        assert (
            instance.inside("http://loki:3100/config", body=True) == engine.replacement
        )
        argv, _, timeout = engine.calls[-1]
        assert argv[argv.index("curl") :] == [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            "-m",
            "10",
            "http://loki:3100/config",
        ]
        assert timeout == 15
    engine.replacement = Result(0, "A" * smoke.LIMIT)
    assert instance.inside("http://loki:3100/config", body=True) == engine.replacement
    engine.replacement = Result(0, "A" * smoke.LIMIT, "b")
    with pytest.raises(command.SmokeError, match="output exceeds 4 MiB"):
        instance.inside("http://loki:3100/config", body=True)


def test_container_ids_and_missing_ps() -> None:
    _, engine, instance = prepared()
    states = instance.container_ids()
    assert states == {name: (name, "2026-10-25T05:38:26Z") for name in smoke.SERVICES}
    for replacement in (Result(17, "private"), Result(0, "")):
        engine.fail_match = "ps -q"
        engine.replacement = replacement
        with pytest.raises(command.SmokeError, match="container state unavailable"):
            instance.container_ids()


@pytest.mark.parametrize("what", ["ready", "healthy"])
@pytest.mark.parametrize("code", [0, 17])
def test_readiness_and_health_poll_failure(what: str, code: int) -> None:
    _, engine, instance = prepared()
    engine.fail_match = "exec" if what == "ready" else "{{.State.Health.Status}}"
    engine.replacement = Result(code, "private")
    with pytest.raises(
        command.SmokeError,
        match="did not become ready" if what == "ready" else "is not healthy",
    ):
        instance.group3()
    assert instance.system.monotonic() == (180 if what == "ready" else 120)


def test_utility_refuses_outside_root_and_empty_reclaim() -> None:
    fs, _, instance = prepared()
    other = replace(instance, data=Path("/unsafe"))
    with pytest.raises(command.SmokeError, match="unsafe data dir"):
        other.utility(["rm", "-rf", "/unsafe"])
    other = replace(instance, system=replace(instance.system, platform="darwin"))
    with pytest.raises(command.SmokeError, match="unsafe data dir"):
        other.utility(["rm", "-rf", "/unsafe"])
    command.render.remove_tree(fs, HOST / "secrets")
    instance.reclaim()


def test_finalized_transcript_idempotent_and_artifact_scan() -> None:
    fs, _, instance = prepared()
    instance.emit("last stage output")
    instance.finalize_transcript()
    before = list(fs.calls)
    instance.finalize_transcript()
    assert fs.calls == before
    assert fs.read_text(OUT / "transcript.log") == "smoke setup\nlast stage output\n"
    fs.mkdir(ARTIFACTS)
    instance.scans()
    fs.write_atomic(ARTIFACTS / "existing", "clean", 0o600)
    fs.write_atomic(OUT / "empty.log", "", 0o600)
    instance.scans()
    fs.write_atomic(ARTIFACTS / "existing", instance.sentinels.token, 0o600)
    with pytest.raises(command.SmokeError, match="sentinel secret: existing"):
        instance.scans()


@pytest.mark.parametrize("error", ["result", "exception"])
def test_cleanup_attempts_every_step(
    error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, instance = prepared()
    if error == "result":
        engine.fail_match = "down -v --remove-orphans"
    else:

        def fail(*_args: object, **_kwargs: object) -> Result:
            raise OSError

        monkeypatch.setattr(instance, "compose", fail)
    assert instance.cleanup() is False
    assert fs.lstat(HOST) is None
    assert fs.lstat(OUT) is None
    removals = [argv[-1] for argv, _, _ in engine.calls if argv[1:3] == ["rm", "-f"]]
    assert removals == [instance.project + "-" + name for name in command.FIXTURES]


def test_generic_error_before_and_after_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, _, system = setup()

    def fail() -> Path:
        raise OSError

    monkeypatch.setattr(fs, "temporary_directory", fail)
    assert run(system) == (
        1,
        "FAIL smoke: operation unavailable (OSError; before group 1)\n",
    )
    fs, engine, system = setup()
    engine.fail_match = "config --format json"
    engine.replacement = Result(0, "invalid json")
    code, output = run(system)
    assert code == 1
    assert (
        "FAIL smoke: operation unavailable (JSONDecodeError; before group 1, step 4)\n"
        in output
    )
    assert fs.lstat(OUT) is None


def test_stage_forwards_keyword_arguments() -> None:
    _, _, instance = prepared()

    def operation(*, source: Path) -> None:
        assert source == SOURCE

    instance.stage("group 2", operation, source=SOURCE)


@pytest.mark.parametrize(
    ("error", "label"),
    [
        (ValueError("private-error-sentinel"), "ValueError"),
        (
            ValueError("init: can't inspect 'grafana/grafana.db'; plan incomplete"),
            "ValueError",
        ),
        (KeyError("private-error-sentinel"), "KeyError"),
        (TypeError("private-error-sentinel"), "TypeError"),
        (command.CliError("private-error-sentinel"), "CliError"),
        (
            command.initialization.PlanIncompleteError("private-error-sentinel"),
            "PlanIncompleteError",
        ),
        *[
            (command.initialization.PlanIncompleteError(message), "PlanIncompleteError")
            for message in (
                "init: can't inspect grafana/grafana.db; plan incomplete",
                "init: can't inspect 'grafana/grafana.db'; plan incomplete\n",
                (
                    "private-error-sentinel init: can't inspect "
                    "'grafana/grafana.db'; plan incomplete"
                ),
                (
                    "init: can't inspect 'grafana/grafana.db'; plan incomplete "
                    "private-error-sentinel"
                ),
                (
                    "init: can't inspect 'grafana/\nprivate-error-sentinel'; "
                    "plan incomplete"
                ),
            )
        ],
        (OSError("private-error-sentinel"), "OSError"),
        (
            OSError(errno.ENOENT, "private-error-sentinel", "/private-file-sentinel"),
            "FileNotFoundError [ENOENT]",
        ),
        (OSError(-1, "private-error-sentinel", "/private-file-sentinel"), "OSError"),
    ],
)
def test_operation_failure_is_safe_and_identifies_group_step(
    error: Exception, label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine, system = setup()
    original = engine.run

    def fail(
        argv: list[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        # The copy is the first command in group 2; permissions are the second.
        if "a+rX" in argv:
            raise error
        return original(argv, env, timeout, input_data=input_data)

    monkeypatch.setattr(engine, "run", fail)
    code, output = run(system)
    assert code == 1
    assert f"FAIL smoke: operation unavailable ({label}; group 2, step 2)\n" in output
    assert "private-error-sentinel" not in output
    assert "private-file-sentinel" not in output
    assert "ok 1:" in output
    assert "ok 2:" not in output


@pytest.mark.parametrize(
    "relative",
    ["grafana/grafana.db", "grafana/operator's.db", "grafana/both'\"\\\n.db"],
)
def test_inspect_failure_preserves_only_data_relative_message(
    relative: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, system = setup()

    def denied(path: Path) -> NoReturn:
        raise PermissionError(
            errno.EACCES, "private-error-sentinel", str(path) + "private-file-sentinel"
        )

    def fail(instance: command.Smoke) -> None:
        instance.step = 20
        command.initialization._inspect(  # exercise the real safe exception boundary
            denied, instance.data / relative, instance.data, instance.system.euid
        )

    monkeypatch.setattr(command.Smoke, "group2", fail)
    code, output = run(system)
    assert code == 1
    failures = [line for line in output.splitlines() if line.startswith("FAIL ")]
    assert failures == [
        (
            "FAIL smoke: operation unavailable "
            "(PlanIncompleteError; group 2, step 20): "
            f"init: can't inspect {relative!r}; plan incomplete"
        )
    ]
    assert "private-error-sentinel" not in output
    assert "private-file-sentinel" not in output
    assert "/var/lib/" not in output
    assert str(HOST) not in output


@pytest.mark.parametrize(
    ("module", "name", "group"),
    [
        (command.Smoke, "group1", "group 1"),
        (command.Smoke, "group2", "group 2"),
        (command.Smoke, "group3", "group 3"),
        (command.Smoke, "group3a", "group 3a"),
        (smoke_stages, "group4", "group 4"),
        (smoke_stages, "group5", "group 5"),
        (smoke_stages, "export_persistence", "export persistence"),
        (smoke_capacity, "smoke", "capacity"),
        (smoke_stages, "group6", "group 6"),
        (smoke_checks, "group7", "group 7"),
        (smoke_lifecycle, "group8", "group 8"),
        (smoke_lifecycle, "group9", "group 9"),
        (smoke_lifecycle, "group10", "group 10"),
        (smoke_checks, "group11", "group 11"),
        (smoke_checks, "group12", "group 12"),
        (smoke_lifecycle, "group13", "group 13"),
        (smoke_capacity, "cleanup", "capacity cleanup"),
    ],
)
def test_each_stage_reports_its_own_group_and_resets_step(
    module: object, name: str, group: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, system = setup()

    def fail(_run: command.Smoke, *_args: object) -> None:
        message = "private-error-sentinel"
        raise KeyError(message)

    monkeypatch.setattr(module, name, fail)
    code, output = run(system)
    assert code == 1
    assert f"FAIL smoke: operation unavailable (KeyError; {group}, step 1)\n" in output
    assert "private-error-sentinel" not in output


def test_diagnostics_exceptions_and_original_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, system = setup()

    def fail(_self: command.Smoke) -> None:
        rule = "fixture failure"
        raise command.SmokeError(rule, 23)

    def disk_fail(_self: command.Smoke) -> str:
        raise OSError

    monkeypatch.setattr(command.Smoke, "group3", fail)
    monkeypatch.setattr(command.Smoke, "disk_counts", disk_fail)
    code, output = run(system)
    assert code == 1  # collection failed during group 2, before the injected stage
    assert "disk evidence unavailable" in output


def test_cleanup_before_output_and_early_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, engine, system = setup()
    engine.interrupt_at = 1
    code, output = run(system)
    assert code == 130
    assert fs.lstat(HOST) is None
    assert "capture unavailable" in output
    fs, _, system = setup()

    def interrupted(
        _args: argparse.Namespace, _system: cli.System, _out: io.StringIO
    ) -> int:
        interrupt()

    # Directly test that the dispatch context restores handlers on BaseException.
    monkeypatch.setattr(command, "_dispatch", interrupted)
    previous = signal.getsignal(signal.SIGINT)
    with pytest.raises(command.SmokeError) as failure:
        run(system)
    assert failure.value.status == 130
    assert signal.getsignal(signal.SIGINT) is previous


def test_capture_stderr_scanned() -> None:
    fs, engine, system = setup()
    engine.fail_match = "up -d"
    engine.log_error = "diagnostic stderr"
    code, output = run(system)
    assert code == 1
    assert "clean Alloy" in output
    assert fs.list_dir(ARTIFACTS) == ("alloy.log", "loki-head.log", "loki.log")


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_early_signal_interrupt(number: int, monkeypatch: pytest.MonkeyPatch) -> None:
    fs, _, system = setup()

    def fail() -> Path:
        interrupt(number)

    monkeypatch.setattr(fs, "temporary_directory", fail)
    assert run(system) == (128 + number, "FAIL interrupted\n")


def test_present_missing_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, instance = prepared()
    engine.fail_match = "s3probe buckets"
    engine.replacement = Result(0, "invalid")
    with pytest.raises(command.SmokeError, match="bucket inventory differs"):
        instance.inventory()
    fs.modes[HOST / "secrets/missing"] = stat.S_IFREG
    original = fs.lstat

    def missing(path: Path) -> object:
        return None if path.name in ("missing", "compose.yaml") else original(path)

    monkeypatch.setattr(fs, "lstat", missing)
    with pytest.raises(command.SmokeError, match="fingerprint unavailable"):
        instance.fingerprint()
    with pytest.raises(command.SmokeError, match="rendered file unavailable"):
        instance._private_tree(OUT)  # a disappearing rendered file must fail


def test_original_status_survives_capture_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, system = setup()

    def fail(_self: command.Smoke) -> None:
        rule = "fixture failure"
        raise command.SmokeError(rule, 23)

    monkeypatch.setattr(command.Smoke, "group3", fail)
    code, output = run(system)
    assert code == 23
    assert "clean Alloy and Loki logs retained" in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_compose_and_utility_command_contract(platform: str) -> None:
    fs, engine, system = setup()
    if platform == "darwin":
        engine.identity = "docker-desktop|Docker Desktop"
        system = replace(system, platform=platform)
    assert run(system)[0] == 0
    counts = Counter((tuple(argv), timeout) for argv, _, timeout in engine.calls)
    actual = [
        {"argv": list(argv), "timeout": timeout, "count": count}
        for (argv, timeout), count in counts.items()
    ]
    fixture = Path(__file__).parent / "testdata/smoke-commands.json"
    expected = [
        record
        for record in json.loads(fixture.read_text())
        if record.pop("platform", platform) == platform
    ]
    assert actual == expected
    for call in fs.calls:
        if (
            call[0] == "write"
            and OUT in call[1].parents
            and call[1].name.endswith((".log", "fixture.yaml"))
        ):
            # render writes public-readable configs, then smoke makes them private;
            # everything created by smoke itself starts private.
            assert call[2] == 0o600


@pytest.mark.parametrize(
    ("match", "rule"),
    [
        ("config -q", "setup: model does not load without secrets"),
        ("config --format json", "setup: model unavailable"),
        ("down -v --remove-orphans", "setup: leftover project cleanup failed"),
        ("--entrypoint chown", "init: host ownership unavailable"),
        ("--entrypoint chmod", "init: mode update failed"),
        ("cp rustfs:pinned -a /src/.", "2: config copy failed"),
        ("chmod rustfs:pinned -R", "2: config permissions failed"),
        ("up -d alert-stub", "2: alert stubs failed"),
        ("ps -a -q s3-buckets", "2: s3-buckets did not exit 0"),
        ("logs --no-log-prefix s3-buckets", "2: bucket bootstrap failed"),
        ("run --rm -T s3-buckets", "2: bucket bootstrap failed"),
        ("s3probe buckets", "2: bucket inventory unavailable"),
        ("s3probe delete-bucket", "2: partial bootstrap fixture failed"),
        ("logs --no-log-prefix rustfs", "2: rustfs start log unavailable"),
        ("s3probe default-credentials", "2: default login was not refused"),
        ("up -d --wait --wait-timeout 180", "3a: unmodified up --wait failed"),
        ("rm -sf readiness-unhealthy", "3a: fixture removal failed"),
        ("rm -sf readiness-failed", "3a: fixture removal failed"),
    ],
)
def test_stage_command_failure_rules(match: str, rule: str) -> None:
    fs, engine, system = setup()
    engine.fail_match = match
    code, output = run(system)
    assert code == 1
    assert "FAIL " + rule + "\n" in output
    assert "untrusted" not in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None


@pytest.mark.parametrize(
    ("match", "result", "rule"),
    [
        ("s3probe buckets", Result(0, "bad"), "2: bucket inventory differs"),
        (
            "logs --no-log-prefix s3-buckets",
            Result(0, "bad"),
            "2: bucket bootstrap output",
        ),
        (
            "logs --no-log-prefix rustfs",
            Result(0, "rustfsadmin"),
            "2: rustfs default login",
        ),
        (
            "{{.State.Status}} {{.State.ExitCode}}",
            Result(0, "running 0"),
            "2: s3-buckets did not exit 0",
        ),
        (
            "http://loki:3100/config",
            Result(0, "ingester:\n  wal:\n    disk_full_threshold: 0.90\n200"),
            "3: Loki WAL threshold: missing or unexpected value",
        ),
        (
            "http://loki:3100/config",
            Result(17, "private"),
            "3: Loki WAL threshold: configuration unavailable",
        ),
        (
            "ps -a -q readiness-unhealthy",
            Result(0, ""),
            "3a: fixture failed for an unintended cause",
        ),
        (
            "{{.State.Health.Status}} readiness-unhealthy",
            Result(0, "healthy"),
            "3a: fixture failed for an unintended cause",
        ),
        (
            "{{.State.Status}}:{{.State.ExitCode}} readiness-failed",
            Result(0, "exited:1"),
            "3a: fixture failed for an unintended cause",
        ),
        (
            "up -d --wait --wait-timeout 30",
            Result(0, "private"),
            "3a: up --wait accepted fixture or timed out",
        ),
        (
            "up -d --wait --wait-timeout 30",
            Result(None, "private"),
            "3a: up --wait accepted fixture or timed out",
        ),
    ],
)
def test_stage_parsed_failure_rules(match: str, result: Result, rule: str) -> None:
    fs, engine, system = setup()
    engine.fail_match = match
    engine.replacement = result
    code, output = run(system)
    assert code == 1
    assert "FAIL " + rule + "\n" in output
    assert "private" not in output
    assert fs.lstat(OUT) is None


def test_failed_up_sentinel_names_file() -> None:
    fs, engine, system = setup()
    engine.fail_match = "up -d"
    engine.replacement = Result(17, "C" * 40)
    code, output = run(system)
    assert code == 1
    assert "sentinel secret: up.log" in output
    assert "C" * 40 not in output
    assert fs.list_dir(ARTIFACTS) == ()


def test_transcript_private_write_and_init_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, instance = prepared()
    instance.finalize_transcript()
    assert ("write", OUT / "transcript.log", 0o600) in fs.calls

    def refusal(_facts: init.Facts, _owner: tuple[int, int]) -> init.Refusal:
        return init.Refusal("synthetic refusal")

    monkeypatch.setattr(init, "plan", refusal)
    with pytest.raises(command.SmokeError, match=r"^1: init failed$"):
        instance.initialize_and_sync()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("missing", "scan unavailable: candidate"),
        ("fifo", "scan unavailable: candidate"),
        ("empty", "scan empty: candidate"),
    ],
)
def test_scan_error_wording(kind: str, expected: str) -> None:
    fs = Fake()
    path = Path("/candidate")
    if kind == "fifo":
        fs.modes[path] = stat.S_IFIFO
    elif kind == "empty":
        fs.write_atomic(path, b"", 0o600)
    with pytest.raises(command.SmokeError) as failure:
        command.scan_path(fs, path, (b"secret",))
    assert str(failure.value) == expected


def test_private_render_and_host_init_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, instance = prepared()
    for path in fs.modes:
        if path == OUT or OUT in path.parents:
            assert fs.modes[path] & 0o777 == (
                0o700 if stat.S_ISDIR(fs.modes[path]) else 0o600
            )
    env_text = fs.read_text(OUT / "release/.env")
    assert "O11Y_CONFIG_GRAFANA=/temporary/config/grafana-" in env_text
    assert "O11Y_CONFIG_LOKI=/temporary/config/loki-" in env_text
    assert isinstance(instance.initialize(), init.Plan)
    for spec in init.DIRECTORIES:
        assert fs.owners[HOST / spec.name] == (123, 456)
    for spec in init.SECRETS:
        assert fs.owners[HOST / "secrets" / spec.name] == (123, 456)
    instance.seed("new", b"X")
    assert fs.modes[HOST / "secrets/new"] & 0o777 == 0o600
    # init's access-sensitive gather must get the actual effective uid.
    original = command.initialization.gather

    def gather(filesystem: Filesystem, path: Path, euid: int) -> init.Facts:
        assert euid == 123
        return original(filesystem, path, euid)

    monkeypatch.setattr(command.initialization, "gather", gather)
    assert isinstance(instance.initialize(), init.Plan)


@pytest.mark.parametrize("image", ["", 17, None])
def test_invalid_model_image(image: object) -> None:
    _, engine, system = setup()
    document = model()
    document["services"]["rustfs"]["image"] = image
    engine.fail_match = "config --format json"
    engine.replacement = Result(0, json.dumps(document))
    code, output = run(system)
    assert code == 1
    assert "FAIL setup: utility image unavailable\n" in output


@pytest.mark.parametrize(
    ("match", "rule"),
    [
        ("rm rustfs:pinned -rf /d/smoke", "setup: leftover data cleanup failed"),
        ("cp rustfs:pinned -a /host/.", "init: data copy failed"),
        (
            "cp rustfs:pinned -a /d/smoke-01010101/rustfs/.rustfs.sys",
            "2: RustFS guard copy failed",
        ),
        ("chown rustfs:pinned -R", "2: RustFS guard ownership failed"),
    ],
)
def test_macos_copy_failure_rules(match: str, rule: str) -> None:
    fs, engine, system = setup()
    engine.identity = "docker-desktop|Docker Desktop"
    engine.fail_match = match
    code, output = run(replace(system, platform="darwin"))
    assert code == 1
    assert "FAIL " + rule + "\n" in output
    assert fs.lstat(OUT) is None


@pytest.mark.parametrize(
    ("number", "secret", "replacement", "rule"),
    [
        (2, init.WEBHOOK, b"bad", "1: alert placeholders differ"),
        (2, init.SMTP, b"bad", "1: alert placeholders differ"),
        (4, init.SECRETS[0].name, b"bad", "1: init replaced a valid existing secret"),
        (10, init.SECRETS[0].name, b"sentinel", "1: truncated key was left in place"),
        (12, init.SECRETS[0].name, b"bad", "1: sentinel key could not be restored"),
    ],
)
def test_group1_value_checks(
    number: int,
    secret: str,
    replacement: bytes,
    rule: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, instance = prepared()
    original = instance.values
    calls = 0

    def values() -> dict[str, bytes]:
        nonlocal calls
        calls += 1
        result = original()
        if calls == number:
            result[secret] = (
                dict(instance.sentinels.values)[secret]
                if replacement == b"sentinel"
                else replacement
            )
        return result

    monkeypatch.setattr(instance, "values", values)
    with pytest.raises(command.SmokeError) as failure:
        instance.group1()
    assert str(failure.value) == rule


@pytest.mark.parametrize(
    ("number", "rule"),
    [
        (2, "1: a second init changed a file"),
        (3, "1: interrupted init recovery changed a file"),
        (5, "2: refused init changed a file"),
    ],
)
def test_fingerprint_stage_failures(
    number: int, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, instance = prepared()
    original = instance.fingerprint
    calls = 0

    def fingerprint() -> tuple[tuple[str, int, int, int, int, bytes], ...]:
        nonlocal calls
        calls += 1
        value = original()
        return (*value, ("changed", 0, 0, 0, 0, b"")) if calls == number else value

    monkeypatch.setattr(instance, "fingerprint", fingerprint)
    with pytest.raises(
        command.SmokeError
    ) as failure:  # failure may occur in either ordered stage
        instance.group1()
        instance.group2()
    assert str(failure.value) == rule


def test_consistency_and_layout_failure_rules() -> None:
    fs, engine, instance = prepared()
    instance.group1()
    instance.reclaim()
    fs.write_atomic(HOST / "secrets/s3.env", b"wrong", 0o600)
    with pytest.raises(command.SmokeError) as failure:
        instance.consistent()
    assert str(failure.value) == "1: s3.env does not match the key files"
    for replacement, rule in (
        (Result(17), "1: layout unavailable"),
        (Result(0, "0 0 777 0"), "1: owners, modes or sizes differ from the layout"),
    ):
        engine.fail_match = "stat rustfs:pinned -c"
        engine.replacement = replacement
        with pytest.raises(command.SmokeError) as failure:
            instance.check_layout()
        assert str(failure.value) == rule


def test_bucket_replacement_and_dependency_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for number, expected in (
        (2, "2: a second run recreated a bucket"),
        (3, "2: existing bucket recreated"),
    ):
        _, _, instance = prepared()
        instance.group1()
        original = instance.inventory
        calls = 0

        def inventory(
            original: Callable[[], tuple[str, ...]] = original, number: int = number
        ) -> tuple[str, ...]:
            nonlocal calls
            calls += 1
            value = original()
            return (value[0] + " changed", *value[1:]) if calls == number else value

        monkeypatch.setattr(instance, "inventory", inventory)
        with pytest.raises(command.SmokeError) as failure:
            instance.group2()
        assert str(failure.value) == expected
    _, _, instance = prepared()
    instance.group1()
    instance.model["loki"]["depends_on"]["s3-buckets"]["condition"] = "service_started"
    with pytest.raises(command.SmokeError) as failure:
        instance.group2()
    assert str(failure.value) == "2: backend bucket dependency"


@pytest.mark.parametrize("bad", [init.Refusal("wrong refusal"), init.Plan(())])
def test_group2_requires_init_own_refusal(
    bad: init.Plan | init.Refusal, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, instance = prepared()
    instance.group1()
    original = instance.initialize

    def initialize() -> init.Plan | init.Refusal:
        value = original()
        return bad if isinstance(value, init.Refusal) else value

    monkeypatch.setattr(instance, "initialize", initialize)
    with pytest.raises(command.SmokeError) as failure:
        instance.group2()
    assert str(failure.value) == "2: init did not refuse damaged key in use"


@pytest.mark.parametrize(
    ("response", "passes"), [("200\n", True), ("200 ", False), ("200X", False)]
)
def test_ready_exact_status(response: str, passes: bool) -> None:
    _, engine, instance = prepared()
    engine.fail_match = "exec"
    engine.replacement = Result(0, response)
    if passes:
        instance.wait_ready("loki", 3)
    else:
        with pytest.raises(command.SmokeError, match="3: loki did not become ready"):
            instance.wait_ready("loki", 3)


@pytest.mark.parametrize("service", smoke.SERVICES)
def test_group3_readiness_keeps_group_label(service: str) -> None:
    _, engine, instance = prepared()
    engine.fail_match = smoke.READY[service]
    engine.replacement = Result(0, "503")
    with pytest.raises(
        command.SmokeError, match=rf"^3: {service} did not become ready$"
    ):
        instance.group3()


def test_raw_disk_failure_status_and_binary_logs() -> None:
    fs, engine, instance = prepared()
    engine.disk = Result(17, "100 7")
    assert instance.disk_counts() == ""
    engine.fail_match = "ps -q"
    assert instance.inspect("loki", "template") == Result(1)
    engine.fail_match = "up -d"
    engine.replacement = Result(17, "\udcff", "\udcfe")
    assert instance.logged(["up", "-d"], "raw.log") == engine.replacement
    assert fs.read_bytes(OUT / "raw.log") == b"\xff\xfe"


def loki_responder(
    mode: str, calls: list[tuple[str, bool, tuple[str, ...]]]
) -> Callable[..., Result]:
    def inside(url: str, *, body: bool = False, args: tuple[str, ...] = ()) -> Result:
        if url.endswith("/ready"):
            return Result(0, "200")
        calls.append((url, body, args))
        if mode == "204" or (mode == "warmup" and len(calls) > 1):
            return Result(0, "\n204")
        if mode == "transport":
            return Result(7, "private", "private error")
        if mode == "malformed":
            return Result(0, "private")
        return Result(
            0,
            "Ingester is shutting down /private with secret\n"
            + ("503" if mode == "timeout" else "500"),
        )

    return inside


@pytest.mark.parametrize(
    "mode",
    [
        "204",
        "warmup",
        "stopping",
        "active-throttled",
        "transport",
        "timeout",
        "malformed",
    ],
)
def test_loki_write_probe(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, engine, instance = prepared()
    instance.system = replace(instance.system, epoch=lambda: 1000.25)
    original = instance.inside
    calls: list[tuple[str, bool, tuple[str, ...]]] = []

    inside = loki_responder(mode, calls)

    monkeypatch.setattr(instance, "inside", inside)
    if mode in ("204", "warmup"):
        instance.loki_write_probe()
    else:
        if mode == "active-throttled":
            instance.wait_ready("loki", 3)
        expected = (
            "transport-error"
            if mode == "transport"
            else "malformed-status"
            if mode == "malformed"
            else "write-refused"
        )
        with pytest.raises(command.SmokeError) as failure:
            instance.loki_write_probe()
        assert str(failure.value) == "5: Loki write probe: " + expected
        assert instance.system.monotonic() == 180
    assert len(calls) == (1 if mode == "204" else 2 if mode == "warmup" else 91)
    for url, body, args in calls:
        assert url == "http://loki:3100/loki/api/v1/push"
        assert body is True
        assert args[:5] == ("-X", "POST", "-H", "Content-Type: application/json", "-d")
        assert json.loads(args[-1]) == {
            "streams": [
                {
                    "stream": {"service_name": "o11y-smoke-probe"},
                    "values": [["1000000000000", "o11y-smoke-probe"]],
                }
            ]
        }
    # Exercise the real in-network argument builder with that same payload.
    engine.fail_match = "exec"
    engine.replacement = Result(0, "\n204")
    original(calls[0][0], body=True, args=calls[0][2])
    assert engine.calls[-1][0][-7:] == [
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "-d",
        calls[0][2][-1],
        "http://loki:3100/loki/api/v1/push",
    ]


def test_disk_observation_phases() -> None:
    _, _, instance = prepared()
    instance.record_loki_disk("started")
    instance.record_loki_disk("before-group-5")
    assert instance.transcript[-2:] == [
        "smoke Loki disk usage (started): 0.800000\n",
        "smoke Loki disk usage (before-group-5): 0.800000\n",
    ]


@pytest.mark.parametrize(
    ("out", "artifacts", "source"),
    [
        ("/same", "/same", "/src"),
        ("/parent", "/parent/child", "/src"),
        ("/parent/child", "/parent", "/src"),
        ("/src", "/artifacts", "/src"),
        ("/src/child", "/artifacts", "/src"),
        ("/src", "/artifacts", "/src/child"),
        ("/out", "/src", "/src"),
    ],
)
def test_overlapping_paths_usage(out: str, artifacts: str, source: str) -> None:
    _, engine, system = setup()
    output = io.StringIO()
    args = argparse.Namespace(
        out=Path(out), artifacts=Path(artifacts), source=Path(source)
    )
    assert command.dispatch(args, system, output) == 2
    assert output.getvalue() == "FAIL usage: overlapping paths\n"
    assert engine.calls == []


def test_local_engine_refusal() -> None:
    fs, engine, system = setup()
    engine.identity = "other|Linux"
    code, output = run(system)
    assert code == 1
    assert (
        output == "engine mode: local\nFAIL setup: local engine refused\n"
        "smoke diagnostics: capture unavailable (stack not started)\n"
    )
    assert fs.lstat(HOST) is None


def test_utility_link_inspection_and_owner_failures() -> None:
    _, engine, instance = prepared()
    instance = replace(
        instance,
        system=replace(instance.system, platform="darwin"),
        data=Path("/var/lib/o11y-test/smoke-01010101"),
    )
    engine.fail_match = "--entrypoint test rustfs:pinned -L /d/"
    engine.replacement = Result(0)
    with pytest.raises(command.SmokeError) as failure:
        instance.utility(["rm", "-rf", instance.destination])
    assert str(failure.value) == "data dir symlink or inspection failure"
    engine.fail_match = "no-match"
    assert isinstance(instance.initialize(), init.Plan)
    engine.fail_match = "--entrypoint chown"
    engine.replacement = Result(17)
    with pytest.raises(command.SmokeError) as failure:
        instance.sync_data()
    assert str(failure.value) == "init: owner update failed"


def test_fingerprint_tracks_identity_metadata_and_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, instance = prepared()
    assert isinstance(instance.initialize(), init.Plan)
    original = fs.lstat

    def metadata(path: Path) -> os.stat_result | None:
        value = original(path)
        if value is None:
            return None
        fields = list(value)
        fields[4:6] = [123, 456]
        return os.stat_result(fields, {"st_mtime_ns": 123456})

    monkeypatch.setattr(fs, "lstat", metadata)
    before = instance.fingerprint()
    rows = {row[0]: row[1:] for row in before}
    assert set(rows) == {
        *(spec.name + "/" for spec in init.DIRECTORIES),
        *(spec.name for spec in init.SECRETS),
        init.WEBHOOK,
        init.SMTP,
        "s3.env",
    }
    for name, row in rows.items():
        uid, gid, mode, timestamp, digest = row
        assert (uid, gid) == (123, 456)
        expected_mode = (
            fs.modes[HOST / name]
            if name.endswith("/")
            else fs.modes[HOST / "secrets" / name]
        )
        assert mode == expected_mode
        if name.endswith("/"):
            assert (timestamp, digest) == (0, b"")
        else:
            assert timestamp == (0 if name == "s3.env" else 123456)
            assert len(digest) == 32
    fs.files[HOST / "secrets/rustfs.secret_key"] = "changed"
    assert instance.fingerprint() != before
    del fs.modes[HOST / "loki"]
    with pytest.raises(command.SmokeError) as failure:
        instance.fingerprint()
    assert str(failure.value) == "1: fingerprint unavailable"


def test_group2_restored_values_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, instance = prepared()
    instance.group1()
    original = instance.values
    calls = 0

    def values() -> dict[str, bytes]:
        nonlocal calls
        calls += 1
        value = original()
        if calls == 2:
            value[init.SECRETS[0].name] = b"wrong"
        return value

    monkeypatch.setattr(instance, "values", values)
    with pytest.raises(command.SmokeError) as failure:
        instance.group2()
    assert str(failure.value) == "2: sentinel key could not be restored"


def test_log_names_and_capture_error_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, instance = prepared()
    instance.group1()
    instance.group2()
    instance.group3a()
    assert {name for name in fs.list_dir(OUT) if name.endswith(".log")} == {
        "up.log",
        "stubs.log",
        "wait.log",
        "readiness-unhealthy.log",
        "readiness-failed.log",
    }
    engine.log_error = "\udcff"
    instance.capture_failure_logs()
    assert fs.read_bytes(OUT / "failure-logs/capture.err") == b"\xff" * 3
    assert fs.modes[OUT / "failure-logs/capture.err"] & 0o777 == 0o600
    assert fs.modes[OUT / "failure-logs"] & 0o777 == 0o700
    engine.log_error = ""
    # Stderr from the first service must survive later empty captures and be scanned.
    original = engine.run

    def dirty(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        if "logs" in argv and argv[-1] == "alloy":
            return replace(result, stderr=instance.sentinels.token.decode())
        return result

    monkeypatch.setattr(engine, "run", dirty)
    with pytest.raises(command.SmokeError) as failure:
        instance.capture_failure_logs()
    assert str(failure.value) == "sentinel secret: capture.err"


def test_finish_failures_always_keep_status_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # All output-only branches are exercised directly as well as through dispatch.
    for kind in ("clean", "capture", "scan", "evidence", "finalize", "cleanup"):
        fs, _, instance = prepared()
        instance.group1()
        instance.started = True
        if kind == "capture":

            def capture() -> bytes:
                rule = "diagnostics: capture failed; no logs promoted"
                raise command.SmokeError(rule)

            monkeypatch.setattr(instance, "capture_failure_logs", capture)
        elif kind == "scan":

            def scan_failure() -> bytes:
                raise OSError

            monkeypatch.setattr(instance, "capture_failure_logs", scan_failure)
        elif kind == "evidence":

            def counts() -> str:
                raise OSError

            monkeypatch.setattr(instance, "disk_counts", counts)
        elif kind == "finalize":

            def finalize() -> None:
                raise OSError

            monkeypatch.setattr(instance, "finalize_transcript", finalize)
        elif kind == "cleanup":

            def cleanup() -> bool:
                return False

            monkeypatch.setattr(instance, "cleanup", cleanup)
        output = io.StringIO()
        instance.finish(23, output)
        text = output.getvalue()
        expected = {
            "capture": (
                "smoke diagnostics: diagnostics: capture failed; "
                "no logs promoted; no logs promoted"
            ),
            "scan": "smoke diagnostics: capture or scan failed; no logs promoted",
            "evidence": "smoke diagnostics: disk evidence unavailable",
            "finalize": "smoke diagnostics: transcript finalization failed",
            "cleanup": "smoke diagnostics: cleanup failed",
        }
        if kind != "clean":
            assert expected[kind] in text.splitlines()
        if kind != "evidence":
            assert (
                "smoke diagnostics: Loki disk usage ratio=0.800000 below 0.90"
                in text.splitlines()
            )
            assert (
                "smoke diagnostics: Loki WAL throttling inconclusive "
                "(no transition in head and tail windows)" in text.splitlines()
            )
        if kind != "cleanup":
            assert fs.lstat(OUT) is None
            assert fs.lstat(HOST) is None


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_interrupt_during_random_allocation_cleans_temp(
    cleanup_fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, _, system = setup()

    def random(_size: int) -> bytes:
        interrupt()

    if cleanup_fails:

        def remove(_path: Path) -> None:
            raise OSError

        monkeypatch.setattr(fs, "rmdir", remove)
    code, output = run(replace(system, random_bytes=random))
    assert code == 130
    if cleanup_fails:
        assert output == "FAIL interrupted\nsmoke diagnostics: cleanup failed\n"
    else:
        assert output == "FAIL interrupted\n"
        assert fs.lstat(HOST) is None


def test_capture_failure_rule_and_binary_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, engine, instance = prepared()
    instance.group1()
    engine.fail_match = "logs --no-color --tail 200 alloy"
    engine.replacement = Result(17, "private")
    with pytest.raises(command.SmokeError) as failure:
        instance.capture_failure_logs()
    assert str(failure.value) == "diagnostics: capture failed; no logs promoted"
    engine.fail_match = ""
    original = engine.run

    def binary(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        return (
            replace(result, stdout="\udcff" + result.stdout)
            if "logs" in argv
            else result
        )

    monkeypatch.setattr(engine, "run", binary)
    instance.capture_failure_logs()
    assert fs.read_bytes(ARTIFACTS / "loki-head.log").startswith(b"\xff1\n")


def test_live_linux_init_keeps_service_directory_owners() -> None:
    fs, engine, instance = prepared()
    instance.group1()
    instance.started = True
    fs.mkdir(HOST / "rustfs/.rustfs.sys")
    instance.reclaim()
    for name in ("rustfs", "grafana"):
        expected = next(spec for spec in init.DIRECTORIES if spec.name == name)
        assert fs.owners[HOST / name] == (expected.uid, expected.gid)
        assert fs.modes[HOST / name] & 0o777 == 0o751
    fs.unlink(HOST / "secrets/s3.env")
    assert isinstance(instance.initialize(), init.Plan)
    assert fs.read_bytes(HOST / "secrets/s3.env") == smoke.s3_env(instance.values())
    assert fs.owners[HOST / "rustfs"] == (10001, 10001)
    assert fs.owners[HOST / "grafana"] == (472, 472)
    instance.sync_data()
    for name in ("rustfs", "grafana"):
        assert fs.modes[HOST / name] & 0o777 == 0o750
    engine.fail_match = "chmod rustfs:pinned 751"
    engine.replacement = Result(17)
    with pytest.raises(command.SmokeError) as failure:
        instance.reclaim()
    assert str(failure.value) == "init: host ownership unavailable"


def test_already_removed_fixture_names_are_clean() -> None:
    fs, engine, instance = prepared()
    engine.fail_match = "docker rm -f"
    assert instance.cleanup() is True
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None


def test_smoke_parser_contract(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli.parser()
    flags = ["--source", "/source", "--out", "/out", "--artifacts", "/artifacts"]
    args = parser.parse_args(["o11y", "smoke", *flags])
    assert vars(args) == {
        "group": "o11y",
        "command": "smoke",
        "dedicated_engine": None,
        "source": Path("/source"),
        "out": Path("/out"),
        "artifacts": Path("/artifacts"),
    }
    for index in range(0, len(flags), 2):
        with pytest.raises(SystemExit) as failure:
            parser.parse_args(["o11y", "smoke", *flags[:index], *flags[index + 2 :]])
        assert failure.value.code == 2
        assert flags[index] in capsys.readouterr().err
    with pytest.raises(SystemExit) as failure:
        parser.parse_args(["o11y", "--help"])
    assert failure.value.code == 0
    assert "smoke exercise a disposable local stack" in " ".join(
        capsys.readouterr().out.split()
    )


@pytest.mark.parametrize(
    ("error", "expected", "status"),
    [
        (command.SmokeError("classified failure"), "FAIL classified failure\n", 1),
        (signal.SIGINT, "FAIL interrupted\n", 130),
        (
            OSError(),
            "FAIL smoke: operation unavailable (OSError; group 2, step 1)\n",
            1,
        ),
    ],
)
def test_dispatch_failure_is_retained_in_transcript(
    error: BaseException | int,
    expected: str,
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, system = setup()
    original = command.Smoke.finish
    finished = []

    def group2(_instance: command.Smoke) -> None:
        if isinstance(error, int):
            interrupt(error)
        else:
            raise error

    def finish(instance: command.Smoke, code: int, output: io.StringIO) -> None:
        assert instance.transcript[-1] == expected
        assert code == status
        finished.append(code)
        original(instance, code, output)

    monkeypatch.setattr(command.Smoke, "group2", group2)
    monkeypatch.setattr(command.Smoke, "finish", finish)
    code, output = run(system)
    assert code == status
    assert finished == [status]
    assert output.startswith("engine mode: local\nsmoke setup\nok 1:")
    assert fs.lstat(HOST) is None
    assert fs.lstat(OUT) is None


@pytest.mark.parametrize("kind", ["absent", "empty", "file", "nonempty"])
def test_artifacts_private_directory(kind: str) -> None:
    fs, engine, system = setup()
    if kind == "file":
        fs.write_atomic(ARTIFACTS, "existing", 0o644)
    elif kind != "absent":
        fs.mkdir(ARTIFACTS)
        if kind == "nonempty":
            fs.write_atomic(ARTIFACTS / "existing", "keep", 0o644)
    code, output = run(system)
    if kind in ("file", "nonempty"):
        assert code == 2
        assert "output must be an empty directory" in output
        existing = ARTIFACTS if kind == "file" else ARTIFACTS / "existing"
        assert fs.read_text(existing) == ("existing" if kind == "file" else "keep")
        assert not any("up" in argv for argv, _, _ in engine.calls)
    else:
        assert code == 0, output
        assert fs.list_dir(ARTIFACTS) == ()
        assert fs.modes[ARTIFACTS] & 0o777 == 0o700


def test_macos_data_names_follow_run() -> None:
    for token in (b"\x12\xab\x34\xcd", b"\x56\xef\x78\x90"):
        fs, engine, system = setup()
        engine.identity = "docker-desktop|Docker Desktop"
        original = system.random_bytes

        def random(
            size: int, original: Callable[[int], bytes] = original, token: bytes = token
        ) -> bytes:
            value = original(size)
            return token if size == 4 else value

        code, output = run(replace(system, platform="darwin", random_bytes=random))
        assert code == 0, output
        expected = "/var/lib/o11y-test/smoke-" + token.hex()
        assert {env["O11Y_DATA_DIR"] for _, env, _ in engine.calls} == {expected}
        for argv, _, _ in engine.calls:
            if (
                "--entrypoint" in argv
                and argv[argv.index("--entrypoint") + 1] == "test"
            ):
                assert argv[-1] in (
                    "/parent/o11y-test",
                    "/parent/o11y-test/smoke-" + token.hex(),
                    "/d/smoke-" + token.hex(),
                )
        assert fs.lstat(Path(expected)) is None


def test_linux_mount_is_data_tree() -> None:
    _, engine, instance = prepared()
    assert instance.utility(["chmod", "700", "/d"]).code == 0
    for argv, _, _ in engine.calls:
        if argv[1] == "run":
            assert argv[argv.index("--entrypoint") + 1] != "test"
            assert argv[argv.index("-v") + 1] == "/temporary:/d"
            assert "/:/d" not in argv


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_remove_data_preserves_mount_root_and_siblings(platform: str) -> None:
    fs, engine, instance = prepared()
    if platform == "darwin":
        instance = replace(
            instance,
            system=replace(instance.system, platform=platform),
            data=Path("/var/lib/o11y-test/smoke-01010101"),
        )
        fs.mkdir(instance.data)
    fs.write_atomic(instance.data / "unexpected", "remove", 0o600)
    sibling = instance.data.parent / "sibling"
    fs.write_atomic(sibling, "keep", 0o600)
    names = fs.list_dir(instance.data)
    assert instance.remove_data() == Result(0)
    assert fs.read_text(sibling) == "keep"
    if platform == "linux":
        assert fs.lstat(instance.data) is not None
        assert fs.list_dir(instance.data) == ()
        assert engine.calls[-1][0][-len(names) :] == ["/d/" + name for name in names]
    else:
        assert fs.lstat(instance.data) is None
        assert engine.calls[-1][0][-1] == "/d/smoke-01010101"


def test_compose_head_budget_and_bounds() -> None:
    _, engine, instance = prepared()
    args = ["logs", "--no-color", "loki"]
    result = instance.compose(args, head=True, timeout=37)
    assert result == Result(0, "".join(f"{n}\n" for n in range(1, 201)))
    assert len(engine.head_calls) == 1
    argv, timeout, byte_limit, line_limit = engine.head_calls[0]
    assert argv[-3:] == args
    assert (timeout, byte_limit, line_limit) == (37, 4 * 1024 * 1024, 200)


@pytest.mark.parametrize("dedicated", [None, "unix:///run/dind/docker.sock"])
@pytest.mark.parametrize("leftovers", [False, True])
def test_dedicated_smoke_dispatch(
    dedicated: str | None,
    leftovers: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, engine, system = setup()
    endpoint = "unix:///run/dind/docker.sock"
    fs.modes[Path("/run/dind/docker.sock")] = stat.S_IFSOCK | 0o600
    original = engine.run

    def metadata(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        assert input_data is None
        responses: dict[tuple[str, ...], str] = {
            (
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                "default",
            ): endpoint,
            ("ps", "-aq"): "stopped-container\n" if leftovers else "",
            ("volume", "ls", "-q"): "",
            ("network", "ls", "--format", "{{.Name}}"): "bridge\nhost\nnone\n",
        }
        if tuple(argv[1:]) in responses:
            engine.calls.append((list(argv), dict(env), timeout))
            return Result(0, responses[tuple(argv[1:])])
        return original(argv, env, timeout)

    monkeypatch.setattr(engine, "run", metadata)
    system = replace(
        system,
        env=dict(system.env)
        | {"O11Y_DEDICATED_ENGINE": endpoint, "DEDICATED_ENGINE": endpoint},
    )
    args = [
        "o11y",
        "smoke",
        "--source",
        str(SOURCE),
        "--out",
        str(OUT),
        "--artifacts",
        str(ARTIFACTS),
    ]
    if dedicated is not None:
        args.extend(["--dedicated-engine", dedicated])
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(args, system, out, err)
    output = out.getvalue()
    mode = "local" if dedicated is None else f"dedicated {dedicated}"
    assert output.startswith(f"engine mode: {mode}\n")
    assert output.count("engine mode:") == 1
    assert err.getvalue() == ""
    if dedicated is None or leftovers:
        assert code == 1
        assert "FAIL setup: local engine refused" in output
        assert not any(argv[1] == "compose" for argv, _, _ in engine.calls)
    else:
        assert code == 0, output
        assert "ok 3a:" in output
    if dedicated is None:
        assert all("DOCKER_HOST" not in env for _, env, _ in engine.calls)
    else:
        assert all(env["DOCKER_HOST"] == endpoint for _, env, _ in engine.calls)
