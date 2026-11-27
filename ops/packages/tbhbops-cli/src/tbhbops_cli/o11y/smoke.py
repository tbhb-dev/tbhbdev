"""Run the disposable ops smoke stack through replaceable system seams."""

# pylint: disable=duplicate-code  # independent protocol contracts

import argparse
import errno
import hashlib
import io
import json
import os.path
import re
import signal
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbops.o11y import init, rustfs_probe, smoke, validate
lazy from tbhbops.o11y.validate import Document

from tbhbops_cli.common import CliError
from tbhbops_cli.o11y import init as initialization
from tbhbops_cli.o11y import render
from tbhbops_cli.o11y import validate as validation
from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.runner import Result
lazy from tbhbops_cli.o11y import (
    smoke_capacity,
    smoke_checks,
    smoke_lifecycle,
    smoke_stages,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping
    from typing import TextIO

    from tbhbops_cli.cli import System
    from tbhbops_cli.o11y.filesystem import Filesystem

READY_TIMEOUT = 180
HEALTH_TIMEOUT = 120
FIXTURE_TIMEOUT = 30
CONTROL_TIMEOUT = 120
WAIT_MARGIN = 30
READY_COMPOSE_TIMEOUT = READY_TIMEOUT + WAIT_MARGIN
FIXTURE_COMPOSE_TIMEOUT = FIXTURE_TIMEOUT + WAIT_MARGIN
PULL_TIMEOUT = 600
PROBE_CURL_TIMEOUT = 10
CURL_EXEC_MARGIN = 5
OBJECT_TIMEOUT = 900
FIXTURES = ("readiness-unhealthy", "readiness-failed", "readiness-consumer")


class SmokeError(Exception):
    """Only a fixed rule, never external text, may reach the transcript."""

    def __init__(self, rule: str, status: int = 1) -> None:
        super().__init__(rule)
        self.status = status


def require(passed: bool, rule: str) -> None:  # assert a classified result
    if not passed:
        raise SmokeError(rule)


def present[T](value: T | None, rule: str) -> T:
    if value is None:
        raise SmokeError(rule)
    return value


def environment(
    inherited: Mapping[str, str], data: Path, owner: tuple[int, int]
) -> dict[str, str]:
    env = validate.docker_environment(inherited)
    # Ignore context overrides as well as DOCKER_HOST before verifying the engine.
    env.pop("DOCKER_CONTEXT", None)
    return {
        **env,
        **smoke.FIXED_ENV,
        "O11Y_DATA_DIR": str(data),
        "O11Y_SECRETS_OWNER": f"{owner[0]}:{owner[1]}",
    }


def safe_path(fs: Filesystem, path: Path) -> None:
    for part in (*reversed(path.parents), path):
        info = fs.lstat(part)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise SmokeError(smoke.FAILURES["usage_symlink_path_refused"], 2)


def private_directory(fs: Filesystem, path: Path) -> None:
    safe_path(fs, path)
    info = fs.lstat(path)
    if info is not None and (not stat.S_ISDIR(info.st_mode) or fs.list_dir(path)):
        raise SmokeError(smoke.FAILURES["usage_output_must_be_an_empty_directory"], 2)
    fs.prepare_output(path)
    fs.chmod(path, 0o700)


def scan_path(fs: Filesystem, path: Path, needles: tuple[bytes, ...]) -> None:
    """Refuse unreadable, missing, linked and vacuous search targets."""
    safe_path(fs, path)
    files = _scan_files(fs, path)
    require(any(files.values()), f"scan empty: {path.name}")
    for name, content in files.items():
        if content:
            rule = smoke.scan(content, needles)
            require(rule is None, f"{rule}: {name.name}")


def _scan_files(fs: Filesystem, path: Path) -> dict[Path, bytes]:
    info = present(fs.lstat(path), f"scan unavailable: {path.name}")
    if stat.S_ISDIR(info.st_mode):
        files = {}
        for name in fs.list_dir(path):
            files.update(_scan_files(fs, path / name))
        return files
    require(stat.S_ISREG(info.st_mode), f"scan unavailable: {path.name}")
    return {path: fs.read_bytes(path)}


def _terminated(number: int, _frame: object) -> None:
    raise SmokeError(smoke.FAILURES["interrupted"], 128 + number)


@contextmanager
def signals() -> Generator[None]:
    previous = {}
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, _terminated)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@dataclass
class Smoke:
    system: System
    out: Path
    artifacts: Path
    host: Path
    data: Path
    project: str
    sentinels: smoke.Sentinels
    env: dict[str, str]
    image: str = ""
    model: dict[str, Document] = field(default_factory=dict)
    started: bool = False
    transcript: list[str] = field(default_factory=list)
    finalized: bool = False
    output_ready: bool = False
    current_values: dict[str, bytes] = field(default_factory=dict)
    release: Path | None = None
    dedicated: str | None = None
    group: str = "before group 1"
    step: int = 1

    @property
    def fs(self) -> Filesystem:
        return self.system.filesystem

    @property
    def linux(self) -> bool:
        return self.system.platform == "linux"

    def emit(self, line: str) -> None:
        self.transcript.append(line + "\n")
        self.finalized = False

    def finalize_transcript(self) -> None:
        if not self.finalized:
            self.fs.write_atomic(
                self.out / "transcript.log", "".join(self.transcript), 0o600
            )
            self.finalized = True

    def stage[**P](
        self,
        group: str,
        operation: Callable[P, None],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        self.group = group
        self.step = 1
        operation(*args, **kwargs)

    def run(
        self,
        argv: list[str],
        timeout: int = CONTROL_TIMEOUT,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = self.system.runner.run(argv, self.env, timeout, input_data=input_data)
        require(
            len(result.stdout.encode(errors="surrogateescape"))
            + len(result.stderr.encode(errors="surrogateescape"))
            <= smoke.LIMIT,
            smoke.FAILURES["output_exceeds_4_mib"],
        )
        # Number command boundaries, never command arguments. In-process work
        # before the next command belongs to that next step as well.
        self.step += 1
        return result

    def compose(  # explicit Compose modes and execution budgets
        self,
        args: list[str],
        *,
        tools: bool = False,
        dummy: bool = False,
        fixture: bool = False,
        timeout: int = CONTROL_TIMEOUT,
        head: bool = False,
        input_data: str | None = None,
    ) -> Result:
        release = self.release or self.out / "release"
        argv = [
            "docker",
            "compose",
            "-p",
            self.project,
            "--env-file",
            str(release / (".env.validate" if dummy else ".env")),
            "-f",
            str(release / "compose.yaml"),
        ]
        if tools:
            argv.extend(
                ["-f", str(release / "compose.tools.yaml"), "--profile", "tools"]
            )
        if fixture:
            argv.extend(["-f", str(self.out / "readiness-fixture.yaml")])
        if head:
            return self.system.runner.read_head(
                [*argv, *args], self.env, timeout, smoke.LIMIT, 200
            )
        return self.run([*argv, *args], timeout, input_data=input_data)

    def utility(
        self, args: list[str], *, host: bool = False, source: Path | None = None
    ) -> Result:
        # The path is checked before *every* root operation, including cleanup.
        if self.linux:
            require(
                self.data == self.host and self.data.parent != self.data,
                smoke.FAILURES["unsafe_data_dir"],
            )
            safe_path(self.fs, self.data)
        else:
            require(
                self.data
                == Path(rustfs_probe.VM_DATA_ROOT) / self.project.removeprefix("o11y-"),
                smoke.FAILURES["unsafe_data_dir"],
            )
            for path in ("/parent/o11y-test", f"/parent/o11y-test/{self.data.name}"):
                check = self.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--log-driver",
                        "none",
                        "--user",
                        "0:0",
                        "--network",
                        "none",
                        "-v",
                        "/var/lib:/parent:ro",
                        "--entrypoint",
                        "test",
                        self.image,
                        "-L",
                        path,
                    ]
                )
                require(
                    check.code == 1,
                    smoke.FAILURES["data_dir_symlink_or_inspection_failure"],
                )
        mount = self.data if self.linux else self.data.parent
        argv = [
            "docker",
            "run",
            "--rm",
            "--log-driver",
            "none",
            "--user",
            "0:0",
            "--network",
            "none",
            "-v",
            f"{mount}:/d",
        ]
        if host:
            argv.extend(["-v", f"{self.host}:/host"])
        if source is not None:
            argv.extend(["-v", f"{source}:/src:ro"])
        if not self.linux:
            # Inspect the child of the VM mount; Linux uses the host lstat above.
            check = self.run(
                [*argv, "--entrypoint", "test", self.image, "-L", self.destination]
            )
            require(
                check.code == 1,
                smoke.FAILURES["data_dir_symlink_or_inspection_failure"],
            )
        return self.run([*argv, "--entrypoint", args[0], self.image, *args[1:]])

    @property
    def destination(self) -> str:
        return "/d" if self.linux else f"/d/{self.data.name}"

    def prepare(self, source: Path) -> None:
        private_directory(self.fs, self.out)
        self.output_ready = True
        private_directory(self.fs, self.artifacts)
        self.emit(smoke.MESSAGES["smoke_setup"])
        render.dispatch(
            argparse.Namespace(
                source=source, out=self.out, config_root=self.data / "config"
            ),
            replace(self.system, env=self.env),
        )
        self._private_tree(self.out)
        for name in ("compose.yaml", "compose.tools.yaml"):
            release = self.out / "release"
            result = self.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    self.project,
                    "--env-file",
                    str(release / ".env.validate"),
                    "-f",
                    str(release / name),
                    "config",
                    "-q",
                ]
            )
            require(
                result.code == 0,
                smoke.FAILURES["setup_model_does_not_load_without_secrets"],
            )
        model = self.compose(["config", "--format", "json"], tools=True, dummy=True)
        require(model.code == 0, smoke.FAILURES["setup_model_unavailable"])
        self.model = json.loads(model.stdout)["services"]
        # ops has no init Compose service; RustFS supplies the root utilities.
        image = self.model["rustfs"]["image"]
        require(
            isinstance(image, str) and bool(image),
            smoke.FAILURES["setup_utility_image_unavailable"],
        )
        self.image = image
        require(
            self.compose(
                ["down", "-v", "--remove-orphans"], tools=True, dummy=True
            ).code
            == 0,
            smoke.FAILURES["setup_leftover_project_cleanup_failed"],
        )
        if not self.linux:
            require(
                self.utility(["rm", "-rf", self.destination]).code == 0,
                smoke.FAILURES["setup_leftover_data_cleanup_failed"],
            )
        self.fs.mkdir(self.host)
        self.fs.mkdir(self.host / "secrets")
        self.env["O11Y_S3_ENV_FILE"] = str(self.host / "secrets/s3.env")

    def _private_tree(self, path: Path) -> None:
        for name in self.fs.list_dir(path):
            child = path / name
            info = present(
                self.fs.lstat(child), smoke.FAILURES["setup_rendered_file_unavailable"]
            )
            if stat.S_ISDIR(info.st_mode):
                self._private_tree(child)
            else:
                self.fs.chmod(child, 0o600)
        self.fs.chmod(path, 0o700)

    def reclaim(self) -> None:
        """Let in-process init inspect Linux files after service ownership checks."""
        if not self.linux:
            return
        directories = (
            ("secrets",) if self.started else tuple(d.name for d in init.DIRECTORIES)
        )
        paths = [self.destination + "/" + name for name in directories]
        paths.extend(
            self.destination + "/secrets/" + n
            for n in (*dict(self.sentinels.values), "s3.env")
        )
        existing = [
            p
            for p in paths
            if self.fs.lstat(self.data / p.removeprefix(self.destination + "/"))
            is not None
        ]
        if existing:
            require(
                self.utility(
                    ["chown", f"{self.system.euid}:{self.system.egid}", *existing]
                ).code
                == 0,
                smoke.FAILURES["init_host_ownership_unavailable"],
            )
        if self.started:
            self.reclaim_guards()

    def reclaim_guards(self) -> None:
        # Preserve service ownership while allowing the host to lstat guards.
        # The private data root still prevents other host users traversing it.
        for name in sorted(
            {Path(s.guard).parts[0] for s in init.SECRETS if s.guard is not None}
        ):
            require(
                self.utility(["chmod", "751", self.destination + "/" + name]).code == 0,
                smoke.FAILURES["init_host_ownership_unavailable"],
            )

    def initialize(self) -> init.Plan | init.Refusal:
        plan = init.plan(
            initialization.gather(self.fs, self.host, self.system.euid),
            (self.system.euid, self.system.egid),
        )
        if isinstance(plan, init.Refusal):
            return plan
        for step in plan.steps:
            if self.linux and self.started and isinstance(step, init.EnsureDir):
                # sync_data applies these owners and modes through the root utility;
                # changing a live backend's owner here would deny it its own data.
                continue
            private = (
                replace(step, uid=self.system.euid, gid=self.system.egid)
                if isinstance(step, (init.EnsureDir, init.SetOwnerMode))
                else step
            )
            initialization.execute(
                private, self.fs, self.host, self.system.choice, io.StringIO()
            )
        return plan

    def sync_data(self) -> None:
        self.current_values = self.values()
        if not self.linux:
            if self.started:
                args = ["cp", "-a", "/host/secrets/.", self.destination + "/secrets/"]
            else:
                args = ["cp", "-a", "/host/.", self.destination + "/"]
            require(
                self.utility(args, host=True).code == 0,
                smoke.FAILURES["init_data_copy_failed"],
            )
        for name, (uid, gid, mode, _) in smoke.layout(
            self.current_values, (self.system.euid, self.system.egid)
        ).items():
            target = self.destination + "/" + name
            require(
                self.utility(["chown", f"{uid}:{gid}", target]).code == 0,
                smoke.FAILURES["init_owner_update_failed"],
            )
            require(
                self.utility(["chmod", f"{mode:o}", target]).code == 0,
                smoke.FAILURES["init_mode_update_failed"],
            )

    def values(self) -> dict[str, bytes]:
        return {
            name: self.fs.read_bytes(self.host / "secrets" / name)
            for name in dict(self.sentinels.values)
        }

    def initialize_and_sync(self) -> None:
        self.reclaim()
        require(
            isinstance(self.initialize(), init.Plan), smoke.FAILURES["1_init_failed"]
        )
        self.sync_data()

    def seed(self, name: str, value: bytes) -> None:
        self.fs.write_atomic(self.host / "secrets" / name, value, 0o600)

    def consistent(self) -> None:
        require(
            self.fs.read_bytes(self.host / "secrets/s3.env")
            == smoke.s3_env(self.values()),
            smoke.FAILURES["1_s3_env_does_not_match_the_key_files"],
        )

    def fingerprint(self) -> tuple[tuple[str, int, int, int, int, bytes], ...]:
        # s3.env is rewritten atomically by ops init, so exclude its timestamp.
        paths = self.fs.list_dir(self.host / "secrets")
        result = []
        for name in paths:
            path = self.host / "secrets" / name
            info = present(
                self.fs.lstat(path), smoke.FAILURES["1_fingerprint_unavailable"]
            )
            stamp = 0 if name == "s3.env" else info.st_mtime_ns
            result.append(
                (
                    name,
                    info.st_uid,
                    info.st_gid,
                    info.st_mode,
                    stamp,
                    hashlib.sha256(self.fs.read_bytes(path)).digest(),
                )
            )
        for directory in init.DIRECTORIES:
            info = present(
                self.fs.lstat(self.host / directory.name),
                smoke.FAILURES["1_fingerprint_unavailable"],
            )
            result.append(
                (directory.name + "/", info.st_uid, info.st_gid, info.st_mode, 0, b"")
            )
        return tuple(result)

    def check_layout(self) -> None:
        expected = smoke.layout(
            self.current_values, (self.system.euid, self.system.egid)
        )
        actual = {}
        for name, (_, _, _, size) in expected.items():
            result = self.utility(
                ["stat", "-c", "%u %g %a %s", self.destination + "/" + name]
            )
            require(result.code == 0, smoke.FAILURES["1_layout_unavailable"])
            uid, gid, mode, length = result.stdout.split()
            actual[name] = (
                int(uid),
                int(gid),
                int(mode, 8),
                None if size is None else int(length),
            )
        rule = smoke.layout_rule(actual, expected)
        require(rule is None, f"1: {rule}")

    def group1(self) -> None:
        values = dict(self.sentinels.values)
        for secret in init.SECRETS:
            self.seed(secret.name, values[secret.name])
        self.initialize_and_sync()
        self.check_layout()
        self.reclaim()
        placeholders = self.values()
        require(
            placeholders[init.WEBHOOK] == init.PLACEHOLDER.encode()
            and placeholders[init.SMTP] == b"",
            smoke.FAILURES["1_alert_placeholders_differ"],
        )
        for name in (init.WEBHOOK, init.SMTP):
            self.seed(name, values[name])
        self.initialize_and_sync()
        self.check_layout()
        self.reclaim()
        require(
            self.values() == values,
            smoke.FAILURES["1_init_replaced_a_valid_existing_secret"],
        )
        self.consistent()
        before = self.fingerprint()
        self.initialize_and_sync()
        self.reclaim()
        require(
            self.fingerprint() == before,
            smoke.FAILURES["1_a_second_init_changed_a_file"],
        )
        self.fs.unlink(self.host / "secrets/s3.env")
        self.seed(".tmp.s3.env", smoke.EMPTY_SECRET)
        self.initialize_and_sync()
        self.reclaim()
        require(
            self.fingerprint() == before,
            smoke.FAILURES["1_interrupted_init_recovery_changed_a_file"],
        )
        self.seed(init.SECRETS[0].name, smoke.EMPTY_SECRET)
        self.initialize_and_sync()
        self.reclaim()
        self.consistent()
        require(
            self.values()[init.SECRETS[0].name] != values[init.SECRETS[0].name],
            smoke.FAILURES["1_truncated_key_was_left_in_place"],
        )
        self.seed(init.SECRETS[0].name, values[init.SECRETS[0].name])
        self.initialize_and_sync()
        self.reclaim()
        require(
            self.values() == values,
            smoke.FAILURES["1_sentinel_key_could_not_be_restored"],
        )
        self.consistent()
        self.sync_data()
        self.emit(f"ok 1: {smoke.SUCCESS['1']}")

    def logged(
        self,
        args: list[str],
        name: str,
        *,
        tools: bool = False,
        fixture: bool = False,
        timeout: int = CONTROL_TIMEOUT,
    ) -> Result:
        result = self.compose(args, tools=tools, fixture=fixture, timeout=timeout)
        self.fs.write_atomic(
            self.out / name,
            (result.stdout + result.stderr).encode(errors="surrogateescape"),
            0o600,
        )
        return result

    def probe(self, args: list[str]) -> Result:
        return self.compose(
            ["run", "--rm", "-T", "s3probe", *args], tools=True, timeout=OBJECT_TIMEOUT
        )

    def inventory(self) -> tuple[str, ...]:
        result = self.probe(["buckets"])
        require(result.code == 0, smoke.FAILURES["2_bucket_inventory_unavailable"])
        parsed = smoke.bucket_inventory(result.stdout)
        return present(parsed, smoke.FAILURES["2_bucket_inventory_differs"])

    def bootstrap(self, created: tuple[str, ...], *, first: bool = False) -> None:
        result = self.compose(
            ["logs", "--no-log-prefix", "s3-buckets"]
            if first
            else ["run", "--rm", "-T", "s3-buckets"]
        )
        require(result.code == 0, smoke.FAILURES["2_bucket_bootstrap_failed"])
        rule = smoke.bucket_log(result.stdout + result.stderr, created)
        require(rule is None, f"2: {rule}")

    def inspect(
        self,
        service: str,
        template: str,
        *,
        fixture: bool = False,
        all_containers: bool = False,
    ) -> Result:
        container = self.compose(
            ["ps", *(["-a"] if fixture or all_containers else []), "-q", service],
            fixture=fixture,
        )
        if container.code != 0 or not container.stdout.strip():
            return Result(1)
        return self.run(["docker", "inspect", "-f", template, container.stdout.strip()])

    def disk_counts(self) -> str:
        result = self.utility(["stat", "-f", "-c", "%b %f", self.destination + "/loki"])
        return result.stdout if result.code == 0 else ""

    def record_loki_disk(self, phase: str) -> None:
        self.emit(smoke.disk_record(self.disk_counts(), phase))

    def group2(self) -> None:
        # Runtime configs get their normal readable modes in the data tree;
        # rendered host output remains private throughout the run.
        require(
            self.utility(
                ["cp", "-a", "/src/.", self.destination + "/config/"],
                source=self.out / "config",
            ).code
            == 0,
            smoke.FAILURES["2_config_copy_failed"],
        )
        require(
            self.utility(["chmod", "-R", "a+rX", self.destination + "/config"]).code
            == 0,
            smoke.FAILURES["2_config_permissions_failed"],
        )
        self.started = True
        require(
            self.logged(["up", "-d"], "up.log", timeout=PULL_TIMEOUT).code == 0,
            smoke.FAILURES["2_up_failed"],
        )
        self.record_loki_disk("started")
        require(
            self.logged(
                ["up", "-d", "alert-stub", "alert-mail"],
                "stubs.log",
                tools=True,
                timeout=PULL_TIMEOUT,
            ).code
            == 0,
            smoke.FAILURES["2_alert_stubs_failed"],
        )
        state = self.inspect(
            "s3-buckets", "{{.State.Status}} {{.State.ExitCode}}", all_containers=True
        )
        require(
            state.code == 0 and state.stdout.strip() == "exited 0",
            smoke.FAILURES["2_s3_buckets_did_not_exit_0"],
        )
        self.bootstrap(smoke.BUCKETS, first=True)
        before = self.inventory()
        self.bootstrap(())
        require(
            self.inventory() == before,
            smoke.FAILURES["2_a_second_run_recreated_a_bucket"],
        )
        require(
            self.probe(["delete-bucket", "mimir-ruler"]).code == 0,
            smoke.FAILURES["2_partial_bootstrap_fixture_failed"],
        )
        self.bootstrap(("mimir-ruler",))
        rule = smoke.kept_buckets(before, self.inventory())
        require(rule is None, f"2: {rule}")
        rule = smoke.dependencies(self.model)
        require(rule is None, f"2: {rule}")
        log = self.compose(["logs", "--no-log-prefix", "rustfs"])
        require(log.code == 0, smoke.FAILURES["2_rustfs_start_log_unavailable"])
        rule = rustfs_probe.start_log(log.stdout + log.stderr)
        require(rule is None, f"2: rustfs {rule}")
        require(
            self.probe(["default-credentials"]).code == 0,
            smoke.FAILURES["2_default_login_was_not_refused"],
        )
        self.reclaim()
        if not self.linux:
            # Compose reads s3.env on the host (compose.tools.yaml:24), while
            # render's default points into the VM (render.py:197).
            require(
                self.utility(
                    [
                        "cp",
                        "-a",
                        self.destination + "/rustfs/.rustfs.sys",
                        "/host/rustfs/",
                    ],
                    host=True,
                ).code
                == 0,
                smoke.FAILURES["2_rustfs_guard_copy_failed"],
            )
            require(
                self.utility(
                    [
                        "chown",
                        "-R",
                        f"{self.system.euid}:{self.system.egid}",
                        "/host/rustfs/.rustfs.sys",
                    ],
                    host=True,
                ).code
                == 0,
                smoke.FAILURES["2_rustfs_guard_ownership_failed"],
            )
        self.seed(init.SECRETS[1].name, smoke.EMPTY_SECRET)
        before_files = self.fingerprint()
        refused = self.initialize()
        require(
            isinstance(refused, init.Refusal)
            and refused.message
            == (
                f"{init.SECRETS[1].name} is missing or damaged and "
                f"{init.SECRETS[1].guard} already exists; "
                "restore the file, nothing was changed"
            ),
            smoke.FAILURES["2_init_did_not_refuse_damaged_key_in_use"],
        )
        require(
            self.fingerprint() == before_files,
            smoke.FAILURES["2_refused_init_changed_a_file"],
        )
        self.seed(
            init.SECRETS[1].name, dict(self.sentinels.values)[init.SECRETS[1].name]
        )
        self.initialize_and_sync()
        self.reclaim()
        require(
            self.values() == dict(self.sentinels.values),
            smoke.FAILURES["2_sentinel_key_could_not_be_restored"],
        )
        self.sync_data()
        self.emit(f"ok 2: {smoke.SUCCESS['2']}")

    def inside(
        self,
        url: str,
        *,
        body: bool = False,
        args: tuple[str, ...] = (),
        input_data: str | None = None,
        curl_timeout: int = PROBE_CURL_TIMEOUT,
    ) -> Result:
        flags = (
            ["-w", "\n%{http_code}"]
            if body
            else ["-o", "/dev/null", "-w", "%{http_code}"]
        )
        return self.compose(
            [
                "exec",
                "-T",
                "rustfs",
                "curl",
                "-s",
                *flags,
                "-m",
                str(curl_timeout),
                *args,
                url,
            ],
            timeout=curl_timeout + CURL_EXEC_MARGIN,
            input_data=input_data,
        )

    def loki_write_probe(self) -> None:
        """Available to group 5 in the next slice; never accepts readiness alone."""
        # poll always overwrites this before it is observed.
        rule = None  # pragma: no mutate

        def writable() -> bool:
            nonlocal rule
            timestamp = str(int(self.system.epoch()) * 1000000000)
            payload = json.dumps(
                {
                    "streams": [
                        {
                            "stream": {"service_name": "o11y-smoke-probe"},
                            "values": [[timestamp, "o11y-smoke-probe"]],
                        }
                    ]
                }
            )
            result = self.inside(
                "http://loki:3100/loki/api/v1/push",
                body=True,
                args=(
                    "-X",
                    "POST",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    payload,
                ),
            )
            rule = smoke.loki_write_rule(result.code, result.stdout)
            return rule is None

        require(
            poll(self.system, READY_TIMEOUT, writable), f"5: Loki write probe: {rule}"
        )

    def wait_ready(self, service: str, group: int) -> None:
        def ready() -> bool:
            result = self.inside(smoke.READY[service])
            return result.code == 0 and result.stdout.rstrip("\n") == "200"

        require(
            poll(self.system, READY_TIMEOUT, ready),
            f"{group}: {service} did not become ready",
        )

    def group3(self) -> None:
        for service in smoke.SERVICES:
            self.wait_ready(service, 3)
        for service in smoke.SERVICES:

            def healthy(name: str = service) -> bool:
                result = self.inspect(name, "{{.State.Health.Status}}")
                return result.code == 0 and result.stdout.strip() == "healthy"

            require(
                poll(self.system, HEALTH_TIMEOUT, healthy),
                f"3: {service} is not healthy",
            )
        self.emit(f"ok 3: {smoke.SUCCESS['3']}")
        response = self.inside("http://loki:3100/config", body=True)
        rule = smoke.wal_threshold(response.code, response.stdout)
        require(rule is None, f"3: Loki WAL threshold: {rule}")
        self.emit(smoke.MESSAGES["smoke_loki_wal_threshold_0_99"])

    def group3a(self) -> None:
        require(
            self.logged(
                ["up", "-d", "--wait", "--wait-timeout", str(READY_TIMEOUT)],
                "wait.log",
                timeout=READY_COMPOSE_TIMEOUT,
            ).code
            == 0,
            smoke.FAILURES["3a_unmodified_up_wait_failed"],
        )
        image = self.model["mimir-health-tools"]["image"]
        fixtures = {
            "readiness-unhealthy": {
                "image": image,
                "container_name": self.project + "-readiness-unhealthy",
                "restart": "unless-stopped",
                "command": ["sleep", "300"],
                "healthcheck": {
                    "test": ["CMD", "false"],
                    "interval": "1s",
                    "timeout": "1s",
                    "retries": 1,
                },
            },
        }
        self.fixture_check(
            fixtures, "readiness-unhealthy", "{{.State.Health.Status}}", "unhealthy"
        )
        fixtures = {
            "readiness-failed": {
                "image": image,
                "container_name": self.project + "-readiness-failed",
                "restart": "no",
                "command": ["sh", "-c", "exit 42"],
            },
            "readiness-consumer": {
                "image": image,
                "container_name": self.project + "-readiness-consumer",
                "restart": "no",
                "command": ["true"],
                "depends_on": {
                    "readiness-failed": {"condition": "service_completed_successfully"}
                },
            },
        }
        self.fixture_check(
            fixtures,
            "readiness-failed",
            "{{.State.Status}}:{{.State.ExitCode}}",
            "exited:42",
        )
        self.fs.unlink(self.out / "readiness-fixture.yaml")
        self.emit(f"ok 3a: {smoke.SUCCESS['3a']}")

    def fixture_check(
        self, fixtures: dict[str, Document], name: str, template: str, expected: str
    ) -> None:
        self.fs.write_atomic(
            self.out / "readiness-fixture.yaml",
            json.dumps({"services": fixtures}),
            0o600,
        )
        up = self.logged(
            ["up", "-d", "--wait", "--wait-timeout", str(FIXTURE_TIMEOUT)],
            name + ".log",
            fixture=True,
            timeout=FIXTURE_COMPOSE_TIMEOUT,
        )
        require(
            up.code is not None and up.code != 0,
            smoke.FAILURES["3a_up_wait_accepted_fixture_or_timed_out"],
        )
        state = self.inspect(name, template, fixture=True)
        require(
            state.code == 0 and state.stdout.strip() == expected,
            smoke.FAILURES["3a_fixture_failed_for_an_unintended_cause"],
        )
        require(
            self.compose(["rm", "-sf", *fixtures], fixture=True).code == 0,
            smoke.FAILURES["3a_fixture_removal_failed"],
        )

    def container_ids(self) -> dict[str, tuple[str, str]]:
        states = {}
        for service in smoke.SERVICES:
            result = self.inspect(service, "{{.Id}} {{.State.StartedAt}}")
            require(result.code == 0, smoke.FAILURES["container_state_unavailable"])
            container, timestamp = result.stdout.split()
            states[service] = container, timestamp
        return states

    def scans(self) -> None:
        needles = self.sentinels.scan_values()
        paths = {self.out / name for name in ("transcript.log", "release", "config")}
        for name in self.fs.list_dir(self.out):
            if name.endswith(".log"):
                path = self.out / name
                if self.fs.read_bytes(path):
                    paths.add(path)
        for path in sorted(paths):
            scan_path(self.fs, path, needles)
        if self.fs.lstat(self.artifacts) is not None and self.fs.list_dir(
            self.artifacts
        ):
            scan_path(self.fs, self.artifacts, needles)

    def capture_failure_logs(self) -> bytes:
        stage = self.out / "failure-logs"
        self.fs.mkdir(stage)
        self.fs.chmod(stage, 0o700)
        logs = {}
        errors = bytes()  # noqa: UP018  # an initially empty error buffer
        for name, args in (
            ("alloy", ["logs", "--no-color", "--tail", "200", "alloy"]),
            ("loki", ["logs", "--no-color", "--tail", "200", "loki"]),
            ("loki-head", ["logs", "--no-color", "loki"]),
        ):
            result = self.compose(args, head=name == "loki-head")
            require(
                result.code == 0,
                smoke.FAILURES["diagnostics_capture_failed_no_logs_promoted"],
            )
            errors += result.stderr.encode(errors="surrogateescape")
            content = result.stdout.encode(errors="surrogateescape")
            logs[name] = b"".join(content.splitlines(keepends=True)[-200:])
            self.fs.write_atomic(stage / (name + ".log"), logs[name], 0o600)
        # Finalize synchronously before any search or publication.
        self.finalize_transcript()
        for name in logs:
            scan_path(self.fs, stage / (name + ".log"), self.sentinels.scan_values())
        if errors:
            self.fs.write_atomic(stage / "capture.err", errors, 0o600)
            scan_path(self.fs, stage / "capture.err", self.sentinels.scan_values())
        self.scans()
        # prepare created this directory; scans above also checks any evidence
        # already retained by a failed stage, such as SMTP delivery.
        self.fs.chmod(self.artifacts, 0o700)
        for name in logs:
            self.fs.rename(stage / (name + ".log"), self.artifacts / (name + ".log"))
        self.emit(
            smoke.MESSAGES["smoke_diagnostics_clean_alloy_and_loki_logs_retained"]
        )
        return logs["loki-head"] + logs["loki"]

    def cleanup(self) -> bool:
        passed = True

        def attempt(call: Callable[[], object]) -> None:
            nonlocal passed
            try:
                result = call()
                if isinstance(result, Result) and result.code != 0:
                    passed = False
            except BaseException:  # noqa: BLE001  # attempt every remaining cleanup even after a signal
                passed = False

        if self.image:
            attempt(
                lambda: self.compose(
                    ["down", "-v", "--remove-orphans"], tools=True, dummy=True
                )
            )
            for name in FIXTURES:
                attempt(lambda name=name: self.remove_fixture(name))
            attempt(self.remove_data)
        attempt(lambda: render.remove_tree(self.fs, self.host))
        if self.output_ready:
            attempt(lambda: render.remove_tree(self.fs, self.out))
        return passed

    def remove_data(self) -> Result:
        # A bind mount root cannot itself be removed. Remove its children,
        # then let the host remove the empty private directory.
        paths = (
            [self.destination + "/" + name for name in self.fs.list_dir(self.data)]
            if self.linux
            else [self.destination]
        )
        return self.utility(["rm", "-rf", *paths])

    def remove_fixture(self, name: str) -> None:
        # down --remove-orphans already removes project fixtures. A missing name
        # is expected here; down's status remains the required engine evidence.
        self.run(["docker", "rm", "-f", self.project + "-" + name])

    def finish(self, status: int, out: TextIO) -> None:
        if status != 0 and self.started:
            logs = bytes()  # noqa: UP018  # an empty buffer, with no meaningful literal to mutate
            try:
                logs = self.capture_failure_logs()
            except SmokeError as exc:
                self.emit(f"smoke diagnostics: {exc}; no logs promoted")
            except BaseException:  # noqa: BLE001  # capture must preserve status and reach cleanup
                self.emit(
                    smoke.MESSAGES[
                        "smoke_diagnostics_capture_or_scan_failed_no_logs_promoted"
                    ]
                )
            try:
                for line in smoke.disk_evidence(self.disk_counts(), logs):
                    self.emit(line)
            except BaseException:  # noqa: BLE001  # evidence must preserve status and reach cleanup
                self.emit(smoke.MESSAGES["smoke_diagnostics_disk_evidence_unavailable"])
        elif status != 0:
            self.emit(
                smoke.MESSAGES[
                    "smoke_diagnostics_capture_unavailable_stack_not_started"
                ]
            )
        try:
            if self.output_ready:
                self.finalize_transcript()
        except BaseException:  # noqa: BLE001  # finalization must preserve status and reach cleanup
            self.emit(
                smoke.MESSAGES["smoke_diagnostics_transcript_finalization_failed"]
            )
        if not self.cleanup():
            self.emit(smoke.MESSAGES["smoke_diagnostics_cleanup_failed"])
        out.write("".join(self.transcript))


def dispatch(args: argparse.Namespace, system: System, out: TextIO) -> int:
    with signals():
        return _dispatch(args, system, out)


def _operation_failure(exc: Exception, run: Smoke | None) -> str:
    error = type(exc).__name__
    if isinstance(exc, OSError) and exc.errno in errno.errorcode:
        error += smoke.MESSAGES["smoke_operation_errno"].format(
            name=errno.errorcode[exc.errno]
        )
    progress = smoke.MESSAGES["smoke_before_group_1"]
    if run is not None:
        progress = smoke.MESSAGES["smoke_operation_progress"].format(
            group=run.group, step=run.step
        )
    line = smoke.MESSAGES["fail_smoke_operation_unavailable"].format(
        error=error, progress=progress
    )
    # Only init._inspect's repr-quoted relative path is safe to disclose.
    if isinstance(exc, initialization.PlanIncompleteError) and re.fullmatch(
        r"""init: can't inspect (?:'(?:[^'\\\r\n]|\\.)*'|"(?:[^"\\\r\n]|\\.)*")"""
        r"; plan incomplete",
        str(exc),
    ):
        line = smoke.MESSAGES["smoke_operation_plan_incomplete"].format(
            failure=line, message=exc
        )
    return line


def _dispatch(
    args: argparse.Namespace, system: System, out: TextIO
) -> int:  # explicit exit and allocation cleanup states
    fs = system.filesystem
    run = None
    temporary = None
    status = 1
    try:
        target, artifacts, source = (
            Path(os.path.abspath(system.cwd / p))  # refuse links first
            for p in (args.out, args.artifacts, args.source)
        )  # resolve would follow links before refusing them
        for path in (target, artifacts):
            safe_path(fs, path)
        if any(
            a == b or a in b.parents or b in a.parents
            for a, b in ((target, artifacts), (target, source), (artifacts, source))
        ):
            raise SmokeError(
                smoke.FAILURES["usage_overlapping_paths"], 2
            )  # usage status is handled with the other safe failures
        temporary = fs.temporary_directory()
        safe_path(fs, temporary)
        project = "o11y-smoke-" + system.random_bytes(4).hex()
        data = Path(
            rustfs_probe.compose_data_dir(system.platform == "linux") or temporary
        )
        if system.platform != "linux":
            data = data.with_name(project.removeprefix("o11y-"))
        env = environment(system.env, data, (system.euid, system.egid))
        sentinels = smoke.sentinels(
            (
                *(system.random_bytes(s.length) for s in init.SECRETS),
                system.random_bytes(16),
                system.random_bytes(16),
            )
        )
        run = Smoke(system, target, artifacts, temporary, data, project, sentinels, env)
        run.dedicated = args.dedicated_engine
        _execute(run, source)
        status = 0
    except SmokeError as exc:
        status = exc.status
        _failure(run, out, f"FAIL {exc}")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        CliError,
        initialization.PlanIncompleteError,
    ) as exc:
        _failure(run, out, _operation_failure(exc, run))
    finally:
        _finish(run, temporary, fs, status, out)
    return status


def _execute(run: Smoke, source: Path) -> None:
    mode = "local" if run.dedicated is None else f"dedicated {run.dedicated}"
    run.emit(f"engine mode: {mode}")
    local = validation.local_engine(run.system, run.env, dedicated=run.dedicated)
    require(local is None, smoke.FAILURES["setup_local_engine_refused"])
    for port in smoke.PORTS:
        if not run.system.ports.available(port):
            raise SmokeError(smoke.FAILURES["usage_loopback_port_already_bound"], 2)
    run.prepare(source)
    run.stage("group 1", run.group1)
    run.stage("group 2", run.group2)
    run.stage("group 3", run.group3)
    run.stage("group 3a", run.group3a)
    run.stage("group 4", smoke_stages.group4, run)
    run.stage("group 5", smoke_stages.group5, run)
    run.stage("export persistence", smoke_stages.export_persistence, run)
    run.stage("capacity", smoke_capacity.smoke, run, source)
    run.stage("group 6", smoke_stages.group6, run)
    run.stage("group 7", smoke_checks.group7, run, source)
    run.stage("group 8", smoke_lifecycle.group8, run, source)
    run.stage("group 9", smoke_lifecycle.group9, run)
    run.stage("group 10", smoke_lifecycle.group10, run)
    run.stage("group 11", smoke_checks.group11, run)
    run.stage("group 12", smoke_checks.group12, run, source)
    run.stage("group 13", smoke_lifecycle.group13, run, source)
    run.stage("capacity cleanup", smoke_capacity.cleanup, run)
    run.finalize_transcript()
    run.scans()


def _finish(
    run: Smoke | None, temporary: Path | None, fs: Filesystem, status: int, out: TextIO
) -> None:
    if run is not None:
        run.finish(status, out)
    elif temporary is not None:
        try:
            render.remove_tree(fs, temporary)
        except BaseException:  # noqa: BLE001  # preserve an allocation or interruption failure
            out.write(smoke.MESSAGES["smoke_diagnostics_cleanup_failed"] + "\n")


def _failure(
    run: Smoke | None, out: TextIO, message: str, *, suffix: str = "\n"
) -> None:
    if run is not None:
        run.emit(message)
    else:
        out.write(message + suffix)
