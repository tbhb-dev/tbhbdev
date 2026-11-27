"""Compose's isolated parser and the production daemon's narrow interface."""

import hashlib
import json
import os
import selectors
import signal
import subprocess  # nosec B404: argv only, empty environment, no shell
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TypedDict, cast

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy import policy
from tbhbops.deploy.config import Service, text
from tbhbops.deploy.protocol import Json, RefusalError, array, decode, mapping, require

VERSION = "5.7.1"
# Docker Compose v5.7.1 Linux downloads, identical to MODULE.bazel's pins.
COMPOSE_SHA256 = {
    "amd64": "af426722a8af5de812fcee30cc1c39f0639f5f723cbadefb25e5d4db054064cf",
    "arm64": "f2f49f903240895fa764c0dca3a14548b754ad9426f6b609ccaf55ec5a3aa2a5",
}


def verify_binary(path: Path) -> None:
    """Hash the binary under a root-owned directory walk before running its path.

    control_read checks the file and every ancestor for root ownership and
    group or other write permission, protecting the later execution by path.
    """
    require(
        hashlib.sha256(fs.control_read(path)).hexdigest() in COMPOSE_SHA256.values(),
        "Compose binary digest mismatch",
    )


SANDBOX = (
    "TemporaryFileSystem=/:ro",
    "MountAPIVFS=no",
    "PrivateNetwork=yes",
    "NoNewPrivileges=yes",
    "CapabilityBoundingSet=",
)


class CommandError(RuntimeError):
    """A child failed, with its potentially secret-bearing output withheld."""


def health_require(condition: bool, reason: str) -> None:  # noqa: FBT001  # readiness predicate
    """Retry unhealthy containers without swallowing safety refusals from probes."""
    if not condition:
        raise CommandError(reason)


OUTPUT_LIMIT = 1024**2
# Chunk size only affects syscall count, never the byte cap or deadline.
READ_CHUNK = 65536  # pragma: no mutate
COMPOSE_TIMEOUT_S = 300
PARSE_TIMEOUT_S = 30
# Docker info reports whether the daemon can enforce each Compose limit.
LIMIT_CAPABILITIES = {
    "pids_limit": "PidsLimit",
    "mem_limit": "MemoryLimit",
    "memswap_limit": "SwapLimit",
}


class HealthBudget(TypedDict, total=False, closed=True):
    timeout_s: float


def health_budget(deadline: float | None) -> HealthBudget:
    if deadline is None:
        return {}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        message = "health gate failed"
        raise CommandError(message)
    return {"timeout_s": remaining}


def read_output(fd: int, output: bytearray, limit: int, *, stdout: bool) -> bool:
    block = os.read(fd, min(READ_CHUNK, limit + 1 - len(output)))
    if not block:
        return False
    if len(output) + len(block) > limit:
        message = "child stdout limit" if stdout else "child stderr limit"
        raise CommandError(message)
    output.extend(block)
    return True


def capture(
    stdout: BinaryIO,
    stderr: BinaryIO,
    deadline: float,
    stdout_limit: int,
    stderr_limit: int,
) -> bytes:
    outputs = {stdout.fileno(): bytearray(), stderr.fileno(): bytearray()}
    limits = {stdout.fileno(): stdout_limit, stderr.fileno(): stderr_limit}
    with selectors.DefaultSelector() as selector:
        selector.register(stdout, selectors.EVENT_READ)
        selector.register(stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            events = selector.select(remaining) if remaining > 0 else []
            if not events:
                message = "child command timeout"
                raise CommandError(message)
            for key, _ in events:
                if not read_output(
                    key.fd,
                    outputs[key.fd],
                    limits[key.fd],
                    stdout=key.fd == stdout.fileno(),
                ):
                    selector.unregister(key.fd)
    return bytes(outputs[stdout.fileno()])


@dataclass(frozen=True)
class Command:
    def __call__(  # noqa: PLR0913  # Separate stream caps and unit cleanup are caller budgets.
        self,
        argv: list[str],
        data: bytes | None = None,
        *,
        timeout_s: float = 60,
        stdout_limit: int = OUTPUT_LIMIT,
        stderr_limit: int = OUTPUT_LIMIT,
        unit: str | None = None,
    ) -> bytes:
        deadline = time.monotonic() + timeout_s
        with tempfile.TemporaryFile() as source:
            source.write(data or b"")
            source.seek(0)
            with subprocess.Popen(  # nosec B603: argv only, no shell
                argv,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={},
                start_new_session=True,
            ) as process:
                try:
                    output = capture(
                        # PIPE guarantees both streams exist.
                        cast("BinaryIO", process.stdout),  # pragma: no mutate
                        cast("BinaryIO", process.stderr),  # pragma: no mutate
                        deadline,
                        stdout_limit,
                        stderr_limit,
                    )
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    self.terminate(process, unit)
                    message = "child command timeout"
                    raise CommandError(message) from None
                except CommandError:
                    self.terminate(process, unit)
                    raise
                if process.returncode:
                    message = "child command failed"
                    raise CommandError(message)
                return output

    def terminate(self, process: subprocess.Popen[bytes], unit: str | None) -> None:
        with suppress(ProcessLookupError):
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        process.wait()
        if unit is not None:
            self.stop_unit(unit)

    def stop_unit(self, unit: str) -> None:
        query = [
            "/usr/bin/systemctl",
            "show",
            "--property=ActiveState",
            "--value",
            unit,
        ]
        try:
            if self(query, timeout_s=10) == b"inactive\n":
                return
            self(["/usr/bin/systemctl", "stop", unit], timeout_s=10)
            state = self(query, timeout_s=10)
        except CommandError, OSError:
            message = "transient unit stop unconfirmed; compensation withheld"
            raise RefusalError(message) from None
        require(
            state == b"inactive\n",
            "transient unit is not inactive; compensation withheld",
        )


@dataclass
class Engine:
    service: Service
    binary: str
    command: Command

    @property
    def state(self) -> Path:
        return self.service.root / "deploy"

    def model_path(self, release: str) -> Path:
        return self.state / "models" / f"{release}.json"

    def compose(self, release: str, source: Path, env: Path) -> list[str]:
        return [
            self.binary,
            "-p",
            self.service.name,
            "--project-directory",
            str(self.service.root / "releases" / release),
            "-f",
            str(source),
            "--env-file",
            str(env),
        ]

    def sandbox(
        self,
        release: str,
        source: Path,
        env: Path,
        *,
        fixed: bool,
        staging: Path | None = None,
    ) -> Json:
        directory = self.service.root / "releases" / release
        binds = [self.binary, str(directory if staging is None else staging)]
        if fixed:
            binds.extend((str(source), "/dev/null"))
        unit = f"tbhb-deploy-parse-{self.service.name}"
        if staging is not None:
            unit += "-" + staging.name
        argv = [
            "/usr/bin/systemd-run",
            "--wait",
            "--pipe",
            "--collect",
            f"--unit={unit}",
        ]
        for property_ in (
            *SANDBOX,
            f"RuntimeMaxSec={PARSE_TIMEOUT_S}",
            "BindReadOnlyPaths=" + " ".join(binds),
        ):
            argv.append("--property=" + property_)  # noqa: PERF401  # ordered unit properties
        argv.extend(self.compose(release, source, env))
        argv.extend(("config", "--no-env-resolution", "--format", "json"))
        return decode(
            self.command(
                argv,
                timeout_s=PARSE_TIMEOUT_S,
                stdout_limit=OUTPUT_LIMIT,
                stderr_limit=OUTPUT_LIMIT,
                unit=unit,
            )
        )

    def parse(self, release: str) -> None:
        directory = self.service.root / "releases" / release
        candidate = self.sandbox(
            release,
            directory / self.service.compose_file,
            directory / self.service.compose_env_file,
            fixed=False,
        )
        policy.model(candidate, self.service)
        self.check_paths(candidate, release, missing=True)
        destination = self.model_path(release)
        fs.write(destination, json.dumps(candidate, sort_keys=True).encode())
        fixed = self.sandbox(release, destination, Path("/dev/null"), fixed=True)
        require(candidate == fixed, "Compose fixed point")
        self.ownership(candidate)

    def preflight(self, release: str, files: dict[str, bytes]) -> None:
        """Validate an unpublished model without changing the service's state."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name, body in files.items():
                path = directory / name.removeprefix("release/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            candidate = self.sandbox(
                release,
                directory / self.service.compose_file,
                directory / self.service.compose_env_file,
                fixed=False,
                staging=directory,
            )
            policy.model(candidate, self.service)
            self.capabilities(candidate)

    def capabilities(self, candidate: Json) -> None:
        """Refuse reports that cannot prove the daemon enforces required limits."""
        try:
            report = mapping(
                decode(
                    self.command(
                        ["/usr/bin/docker", "info", "--format", "{{json .}}"],
                        timeout_s=10,
                        stdout_limit=OUTPUT_LIMIT,
                        stderr_limit=4096,
                    )
                )
            )
        except CommandError, OSError, ValueError:
            message = "daemon capability report unavailable"
            raise RefusalError(message) from None
        for key in ("Driver", "ServerVersion"):
            value = report.get(key)
            require(
                isinstance(value, str) and bool(value.strip()), "daemon unreachable"
            )
        for key in LIMIT_CAPABILITIES.values():
            require(
                isinstance(report.get(key), bool),
                f"daemon capability report invalid: {key}",
            )
        for name, service in mapping(mapping(candidate)["services"]).items():
            required = mapping(service)
            for limit, capability in LIMIT_CAPABILITIES.items():
                require(
                    limit not in required or report[capability] is True,
                    f"daemon cannot enforce {capability} for service {name}",
                )

    def stored(self, release: str) -> Json:
        return decode(fs.read(self.model_path(release)))

    def container_ids(
        self, *, running: bool = False, deadline: float | None = None
    ) -> list[str]:
        output = self.command(
            [
                "/usr/bin/docker",
                "ps",
                "-q" if running else "-aq",
                "--filter",
                f"label=com.docker.compose.project={self.service.name}",
            ],
            **health_budget(deadline),
        )
        ids = output.decode().split()
        require(
            all(identifier.isalnum() for identifier in ids), "container identifiers"
        )
        return ids

    def containers(self, *, deadline: float | None = None) -> list[dict[str, Json]]:
        ids = self.container_ids(deadline=deadline)
        if not ids:
            return []
        result = decode(
            self.command(
                ["/usr/bin/docker", "inspect", *ids], **health_budget(deadline)
            )
        )
        require(isinstance(result, list), "container inspect type")
        return [mapping(entry) for entry in array(result)]

    def stop(self) -> None:
        ids = self.container_ids()
        if ids:
            with suppress(CommandError):
                self.command(["/usr/bin/docker", "stop", "-t", "10", *ids])
            running = self.container_ids(running=True)
            if running:
                self.command(["/usr/bin/docker", "kill", *running])

    def remove(self) -> None:
        """Remove failed containers so a daemon restart cannot revive them."""
        ids = self.container_ids()
        if ids:
            self.command(["/usr/bin/docker", "rm", "-f", *ids])

    def stop_unit(self) -> None:
        for unit in (
            f"tbhb-deploy-{self.service.name}",
            f"tbhb-deploy-parse-{self.service.name}",
        ):
            self.command.stop_unit(unit)

    def check_paths(
        self, candidate: Json, release: str, *, missing: bool = False, live: bool = True
    ) -> None:
        sources = policy.retained(candidate)
        retained: list[policy.Source] = []
        for path in sorted((self.state / "models").glob("*.json")):
            retained.extend(policy.retained(decode(fs.read(path))))
        writable = {source.path for source in (*sources, *retained) if source.writable}
        for container in self.containers() if live else []:
            mounts = container.get("Mounts")
            require(isinstance(mounts, list), "container mounts type")
            for item in array(mounts):
                mount = mapping(item)
                if mount.get("Type") == "bind" and mount.get("RW") is True:
                    writable.add(fs.spelling(text(mount.get("Source"))))
        policy.paths(sources, tuple(retained), writable, self.service, release)
        self.inspect_sources(sources, missing=missing)

    def inspect_sources(
        self, sources: tuple[policy.Source, ...], *, missing: bool
    ) -> None:
        secret_targets = {secret.target for secret in self.service.secrets}
        for source in sources:
            # Init-generated files can only occupy the service's secret directory.
            generated = source.file or not source.writable
            allowed_missing = missing and (
                source.path in secret_targets
                or (
                    generated
                    and source.path.parent == self.service.root / "data" / "secrets"
                )
            )
            fs.inspect(source.path, file=source.file, missing=allowed_missing)

    def ownership(self, model: Json) -> None:
        for kind in ("network", "volume"):
            for entry in mapping(mapping(model).get(kind + "s", {})).values():
                name = text(mapping(entry).get("name"))
                # Listing first distinguishes absence from an inspect failure.
                names = self.command(
                    ["/usr/bin/docker", kind, "ls", "--format", "{{.Name}}"]
                )
                if name not in names.decode().splitlines():
                    continue
                inspected = decode(
                    self.command(["/usr/bin/docker", kind, "inspect", name])
                )
                require(
                    isinstance(inspected, list) and len(inspected) == 1,
                    "resource inspect type",
                )
                labels = mapping(mapping(array(inspected)[0]).get("Labels", {}))
                require(
                    labels.get("com.docker.compose.project") == self.service.name,
                    "foreign resource ownership",
                )

    def run(
        self, release: str, *arguments: str, timeout_s: float = COMPOSE_TIMEOUT_S
    ) -> None:
        argv = [
            "/usr/bin/systemd-run",
            f"--unit=tbhb-deploy-{self.service.name}",
            "--collect",
            "--wait",
            "--pipe",
        ]
        argv.extend(self.compose(release, self.model_path(release), Path("/dev/null")))
        self.command(
            [*argv, *arguments],
            timeout_s=timeout_s,
            stdout_limit=OUTPUT_LIMIT,
            stderr_limit=OUTPUT_LIMIT,
            unit=f"tbhb-deploy-{self.service.name}",
        )

    def local_images(self, release: str) -> bool:
        """A retained digest can restart without registry access when already local."""
        for entry in mapping(mapping(self.stored(release))["services"]).values():
            image = text(mapping(entry)["image"])
            try:
                self.command(["/usr/bin/docker", "image", "inspect", image])
            except CommandError:
                return False
        return True

    def up(self, release: str, *, recreate: bool) -> None:
        # Rollback deliberately keeps the stored model's original policy approval
        # after policy tightening. Host path checks still apply.
        # Recovery has stopped the candidate. Its inspection must not block restart.
        self.check_paths(self.stored(release), release, live=False)
        flags = ["up", "-d", "--wait", "--remove-orphans"]
        if recreate:
            flags.append("--force-recreate")
        self.run(release, *flags)
        self.health(release)

    def health(self, release: str) -> None:
        deadline = time.monotonic() + self.service.timeout_s
        while True:
            try:
                self.check_health(release, deadline=deadline)
                health_budget(deadline)
            except CommandError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    message = "health gate failed"
                    raise CommandError(message) from None
                time.sleep(min(1, remaining))
            else:
                return

    def check_health(self, release: str, *, deadline: float | None = None) -> None:
        if deadline is None:
            deadline = time.monotonic() + self.service.timeout_s
        containers = self.containers(deadline=deadline)
        for name in self.service.health_services:
            selected = [
                container
                for container in containers
                if mapping(mapping(container.get("Config")).get("Labels")).get(
                    "com.docker.compose.service"
                )
                == name
            ]
            health_require(len(selected) == 1, "health container count")
            state = mapping(selected[0].get("State"))
            health_require(state.get("Running") is True, "container is not running")
            if "Health" in state:
                health_require(
                    mapping(state["Health"]).get("Status") == "healthy",
                    "container is not healthy",
                )
        for name, argv in self.service.probes:
            self.run(
                release,
                "exec",
                "-T",
                name,
                *argv,
                **health_budget(deadline),
            )
