"""Run the six RustFS checks in an isolated local Compose project."""

import io
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TextIO
lazy import argparse
lazy from pathlib import Path

from tbhbops.o11y import init, rustfs_probe, validate
from tbhbops_cli.common import CliError
from tbhbops_cli.o11y import init as initialization
from tbhbops_cli.o11y import render
from tbhbops_cli.o11y import validate as validation
lazy from tbhbops_cli.o11y.runner import Handle, Result

if TYPE_CHECKING:
    from collections.abc import Callable

    from tbhbops_cli.cli import System

# Two concurrent probes on one engine collide.
PROJECT = "o11y-probe"
WRITER = "o11y-probe-writer"
CONTROL_TIMEOUT = 120
EXEC_TIMEOUT = 15
OBJECT_TIMEOUT = 900
WRITER_TIMEOUT = 600
POLL_TIMEOUT = 120
INTERVAL = 2
CONNECTION_REFUSED = 7


class ProbeError(Exception):
    """A safe, numbered failure rule."""


def _require(passed: bool, rule: str) -> None:  # noqa: FBT001  # assertion of a result
    if not passed:
        raise ProbeError(rule)


def _poll(system: System, check: Callable[[], bool]) -> bool:
    deadline = system.monotonic() + POLL_TIMEOUT
    while True:
        if check():
            return True
        if system.monotonic() >= deadline:
            return False
        system.sleep(INTERVAL)


def _initialize(system: System, data: Path) -> None:
    """Execute init's plan with private process ownership before root chown."""
    fs = system.filesystem
    fs.mkdir(data)
    owner = (system.euid, system.egid)
    plan = init.plan(initialization.gather(fs, data, system.euid), owner)
    if isinstance(plan, init.Refusal):
        msg = "init failed"
        raise ProbeError(msg)
    for step in plan.steps:
        private = (
            replace(step, uid=owner[0], gid=owner[1])
            if isinstance(step, (init.EnsureDir, init.SetOwnerMode))
            else step
        )
        initialization.execute(private, fs, data, system.choice, io.StringIO())


@dataclass
class Probe:
    system: System
    env: dict[str, str]
    temporary: Path
    linux: bool
    image: str = ""
    writer: Handle | None = None

    def compose(self, args: list[str], timeout: int = CONTROL_TIMEOUT) -> Result:
        release = self.temporary / "release"
        return self.system.runner.run(
            [
                "docker",
                "compose",
                "-p",
                PROJECT,
                "--env-file",
                str(release / ".env"),
                "-f",
                str(release / "compose.yaml"),
                "-f",
                str(release / "compose.tools.yaml"),
                *args,
            ],
            self.env,
            timeout,
        )

    def utility(self, operation: list[str], source: Path | None = None) -> Result:
        return self.system.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--network",
                "none",
                "-v",
                (
                    f"{self.temporary}:/d"
                    if self.linux
                    else f"{rustfs_probe.VM_DATA_ROOT}:/d"
                ),
                *(["-v", f"{source}:/src:ro"] if source is not None else []),
                "--entrypoint",
                operation[0],
                self.image,
                *operation[1:],
            ],
            self.env,
            CONTROL_TIMEOUT,
        )

    def prepare(self, source: Path) -> None:
        release = validation.prepare(
            self.system, source, self.temporary, "http://127.0.0.1:13000/"
        )
        # The dummy env file lets Compose load the model before init creates secrets.
        result = self.system.runner.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(release / ".env.validate"),
                "-f",
                str(release / "compose.yaml"),
                "config",
                "--format",
                "json",
            ],
            self.env,
            CONTROL_TIMEOUT,
        )
        _require(result.code == 0, "could not read model")
        try:
            image = json.loads(result.stdout)["services"]["rustfs"]["image"]
        except ValueError, KeyError, TypeError:
            msg = "could not read RustFS image"
            raise ProbeError(msg) from None
        _require(isinstance(image, str) and bool(image), "could not read RustFS image")
        self.image = image
        data = self.temporary / rustfs_probe.basename("data")
        _initialize(self.system, data)
        self.env["O11Y_S3_ENV_FILE"] = str(data / "secrets/s3.env")
        _require(
            self.compose(["down", "-v", "--remove-orphans"]).code == 0,
            "leftover project could not be removed",
        )
        vm_data = rustfs_probe.compose_data_dir(self.linux)
        if vm_data is not None:
            _require(
                self.utility(["rm", "-rf", "/d/probe"]).code == 0,
                "leftover data could not be removed",
            )
            env_file = release / ".env"
            fs = self.system.filesystem
            settings = fs.read_text(env_file)
            old = f"O11Y_DATA_DIR={self.temporary / 'data'}\n"
            _require(settings.count(old) == 1, "data dir could not be redirected")
            settings = settings.replace(old, f"O11Y_DATA_DIR={vm_data}\n")
            fs.write_atomic(env_file, settings, 0o600)
        if not self.linux:
            _require(
                self.utility(["cp", "-a", "/src/.", "/d/probe/"], data).code == 0,
                "data copy failed",
            )
        destination = "/d/data" if self.linux else "/d/probe"
        rustfs = next(d for d in init.DIRECTORIES if d.name == "rustfs")
        subdir = rustfs_probe.basename(rustfs.name)
        _require(
            self.utility(
                ["chown", "-R", f"{rustfs.uid}:{rustfs.gid}", f"{destination}/{subdir}"]
            ).code
            == 0,
            "init failed",
        )
        secrets = [
            secret
            for secret in init.SECRETS
            if secret.guard == f"{rustfs.name}/.rustfs.sys"
        ]
        _require(bool(secrets), "rustfs secrets could not be found")
        for secret in secrets:
            name = rustfs_probe.basename(secret.name)
            _require(
                self.utility(
                    [
                        "chown",
                        f"{secret.uid}:{secret.gid}",
                        f"{destination}/secrets/{name}",
                    ]
                ).code
                == 0,
                "init failed",
            )

    def start_rule(self) -> str | None:
        log = self.compose(["logs", "--no-color", "rustfs"])
        if log.code != 0:
            return "start log unavailable"
        return rustfs_probe.start_log(log.stdout + log.stderr)

    def checks(self, out: TextIO) -> None:
        _require(
            self.compose(["up", "-d", "rustfs"]).code == 0, "1: rustfs did not start"
        )

        def ready() -> bool:
            result = self.compose(
                [
                    "exec",
                    "-T",
                    "rustfs",
                    "curl",
                    "-s",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "-m",
                    "10",
                    "http://127.0.0.1:9000/health/ready",
                ],
                EXEC_TIMEOUT,
            )
            return result.code == 0 and result.stdout.rstrip("\n") == "200"

        if not _poll(self.system, ready):
            problem = self.start_rule()
            suffix = f" ({problem})" if problem is not None else ""
            msg = f"2: rustfs did not become ready{suffix}"
            raise ProbeError(msg)
        problem = self.start_rule()
        _require(problem is None, f"1: {problem}")
        out.write(f"ok 1: {rustfs_probe.SUCCESS[0]}\n")
        console = self.compose(
            [
                "exec",
                "-T",
                "rustfs",
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-m",
                "5",
                "http://127.0.0.1:9001/",
            ],
            EXEC_TIMEOUT,
        )
        _require(
            console.code == CONNECTION_REFUSED,
            "2: something answers on the console port",
        )
        out.write(f"ok 2: {rustfs_probe.SUCCESS[1]}\n")
        for number, mode, failure in (
            (3, "default-credentials", "the default login was not refused"),
            (4, "objects", "object round trips failed"),
        ):
            result = self.compose(
                ["run", "--rm", "-T", "s3probe", mode], OBJECT_TIMEOUT
            )
            _require(result.code == 0, f"{number}: {failure}")
            out.write(f"ok {number}: {rustfs_probe.SUCCESS[number - 1]}\n")
        self.crash(out)
        result = self.compose(
            ["run", "--rm", "-T", "s3probe", "cleanup"], OBJECT_TIMEOUT
        )
        _require(result.code == 0, "6: the probe bucket could not be removed")
        out.write(f"ok 6: {rustfs_probe.SUCCESS[5]}\n")

    def crash(self, out: TextIO) -> None:
        log = self.temporary / "crash.log"
        self.system.filesystem.write_atomic(log, "", 0o600)
        release = self.temporary / "release"
        self.writer = self.system.runner.start(
            [
                "docker",
                "compose",
                "-p",
                PROJECT,
                "--env-file",
                str(release / ".env"),
                "-f",
                str(release / "compose.yaml"),
                "-f",
                str(release / "compose.tools.yaml"),
                "run",
                "--rm",
                "-T",
                "--name",
                WRITER,
                "s3probe",
                "crash",
            ],
            self.env,
            log,
        )
        _require(
            _poll(
                self.system,
                lambda: "probe: writing" in self.system.filesystem.read_text(log),
            ),
            "5: the writer never started",
        )
        _require(
            self.compose(["kill", "rustfs"]).code == 0, "5: rustfs could not be killed"
        )
        self.system.sleep(INTERVAL)
        _require(
            self.compose(["start", "rustfs"]).code == 0, "5: rustfs did not restart"
        )
        _require(
            self.writer.wait(WRITER_TIMEOUT) == 0, f"5: {rustfs_probe.CRASH_FAILURE}"
        )
        count = rustfs_probe.survivors(self.system.filesystem.read_text(log))
        _require(count is not None, f"5: {rustfs_probe.CRASH_FAILURE}")
        out.write(
            f"ok 5: {count} a kill and restart, and every listed key is readable\n"
        )

    def cleanup(self) -> bool:
        """Attempt every cleanup operation, preserving the required order."""
        passed = True

        def attempt(call: Callable[[], bool]) -> None:
            nonlocal passed
            try:
                if not call():
                    passed = False
            except Exception:  # noqa: BLE001  # every remaining cleanup must run
                passed = False

        writer = self.writer
        if writer is not None:

            def reap() -> bool:
                try:
                    writer.kill()
                finally:
                    code = writer.wait(CONTROL_TIMEOUT)
                return code is not None

            attempt(reap)
        attempt(
            lambda: (
                self.system.runner.run(
                    ["docker", "rm", "-f", WRITER], self.env, CONTROL_TIMEOUT
                ).code
                == 0
            )
        )
        attempt(lambda: self.compose(["down", "-v", "--remove-orphans"]).code == 0)
        if self.image:
            attempt(
                lambda: (
                    self.utility(
                        ["rm", "-rf", "/d/data" if self.linux else "/d/probe"]
                    ).code
                    == 0
                )
            )

        def remove() -> bool:
            render.remove_tree(self.system.filesystem, self.temporary)
            return True

        attempt(remove)
        return passed


def dispatch(args: argparse.Namespace, system: System, out: TextIO) -> int:
    env = validate.docker_environment(system.env)
    dedicated = args.dedicated_engine
    mode = "local" if dedicated is None else f"dedicated {dedicated}"
    out.write(f"engine mode: {mode}\n")
    local = validation.local_engine(system, env, dedicated=dedicated)
    if local is not None:
        out.write(f"FAIL local engine: {local}\n")
        return 1
    try:
        temporary = system.filesystem.temporary_directory()
    except OSError:
        out.write("FAIL probe: temporary directory could not be created\n")
        return 1
    # Local mode refuses Docker Desktop for Linux; dedicated mode uses the
    # named daemon's filesystem through utility containers.
    probe = Probe(system, env, temporary, linux=system.platform == "linux")
    result = 1
    try:
        probe.prepare(args.source)
        probe.checks(out)
        result = 0
    except ProbeError as exc:
        out.write(f"FAIL {exc}\n")
    except (OSError, ValueError, CliError) as exc:
        out.write(f"FAIL probe: {type(exc).__name__}\n")
    finally:
        if not probe.cleanup():
            out.write("FAIL cleanup: probe resources could not be removed\n")
            result = 1
    return result
