"""Parse, store, and run Compose models with deployment safeguards."""

import hashlib
import json
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from tbhbops.deploy import daemon, model, mounts
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.command import OUTPUT_LIMIT, Command, CommandError
from tbhbops.deploy.config import Service, text
from tbhbops.deploy.protocol import Json, array, decode, mapping, require

VERSION = "5.7.1"
# Docker Compose v5.7.1 Linux downloads, identical to MODULE.bazel's pins.
COMPOSE_SHA256 = {
    "amd64": "af426722a8af5de812fcee30cc1c39f0639f5f723cbadefb25e5d4db054064cf",
    "arm64": "f2f49f903240895fa764c0dca3a14548b754ad9426f6b609ccaf55ec5a3aa2a5",
}

SANDBOX = (
    "TemporaryFileSystem=/:ro",
    "MountAPIVFS=no",
    "PrivateNetwork=yes",
    "NoNewPrivileges=yes",
    "CapabilityBoundingSet=",
)

COMPOSE_TIMEOUT_S = 300
PARSE_TIMEOUT_S = 30


def verify_binary(path: Path) -> None:
    """Hash the binary under a root-owned directory walk before running its path.

    control_read checks the file and every ancestor for root ownership and
    group or other write permission, protecting the later execution by path.
    """
    require(
        hashlib.sha256(fs.control_read(path)).hexdigest() in COMPOSE_SHA256.values(),
        "Compose binary digest mismatch",
    )


def health_require(condition: bool, reason: str) -> None:  # readiness predicate
    """Retry unhealthy containers without swallowing safety refusals from probes."""
    if not condition:
        raise CommandError(reason)


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
            argv.append("--property=" + property_)  # ordered unit properties
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
        model.validate(candidate, self.service)
        self.check_paths(candidate, release, missing=True)
        destination = self.model_path(release)
        fs.write(destination, json.dumps(candidate, sort_keys=True).encode())
        fixed = self.sandbox(release, destination, Path("/dev/null"), fixed=True)
        require(candidate == fixed, "Compose fixed point")
        daemon.ownership(self.command, self.service.name, candidate)

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
            model.validate(candidate, self.service)
            daemon.capabilities(self.command, candidate)

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
        sources = mounts.retained(candidate)
        retained: list[mounts.Source] = []
        for path in sorted((self.state / "models").glob("*.json")):
            retained.extend(mounts.retained(decode(fs.read(path))))
        writable = {source.path for source in (*sources, *retained) if source.writable}
        for container in self.containers() if live else []:
            container_mounts = container.get("Mounts")
            require(isinstance(container_mounts, list), "container mounts type")
            for item in array(container_mounts):
                mount = mapping(item)
                if mount.get("Type") == "bind" and mount.get("RW") is True:
                    writable.add(fs.spelling(text(mount.get("Source"))))
        mounts.paths(sources, tuple(retained), writable, self.service, release)
        self.inspect_sources(sources, missing=missing)

    def inspect_sources(
        self, sources: tuple[mounts.Source, ...], *, missing: bool
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
