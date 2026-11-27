"""In-memory filesystem and Docker seams for smoke tests."""

import io
import json
import signal
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NoReturn
lazy from collections.abc import Mapping, Sequence

from tbhbops.o11y import init, smoke
from tbhbops_cli import cli
from tbhbops_cli.o11y import render
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    runtime,
)
from tbhbops_cli.o11y.smoke import setup as setup_stage
from test_o11y_cli import SOURCE, Fake, render_fixture, world
lazy from tbhbops.o11y.validate import Document

OUT = Path("/output")


ARTIFACTS = Path("/artifacts")


HOST = Path("/temporary")


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
                render.remove_tree(self.fs, self.path(raw, argv))
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


def make_system() -> tuple[Fake, Engine, cli.System]:
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


def prepared() -> tuple[Fake, Engine, runtime.Smoke]:
    fs, engine, system = make_system()
    fs.mkdir(HOST)
    sentinels = smoke.sentinels(
        (
            *(bytes([n + 2]) * spec.length for n, spec in enumerate(init.SECRETS)),
            b"t" * 16,
            b"s" * 16,
        )
    )
    instance = runtime.Smoke(
        system,
        OUT,
        ARTIFACTS,
        HOST,
        HOST,
        "o11y-smoke-01010101",
        sentinels,
        runtime.environment(system.env, HOST, (123, 456)),
    )
    setup_stage.prepare(instance, SOURCE)
    return fs, engine, instance
