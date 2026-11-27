"""Check the resolver's source views using isolated Docker bridge clients."""

import re
from dataclasses import dataclass, field
from typing import Protocol, TextIO, TypedDict, cast
lazy import argparse
lazy from collections.abc import Callable, Mapping, Sequence
lazy from pathlib import Path

import yaml

# Official ISC BIND image, 21 MB compressed, with /usr/bin/dig.
# Resolved via registry-1.docker.io's manifest endpoint over HTTPS.
# renovate: datasource=docker depName=internetsystemsconsortium/bind9
DIG_IMAGE = (
    "internetsystemsconsortium/bind9:9.20"
    "@sha256:66092edb53c081fa11b1fa8219e99065b0f497cb38f568ba789702a4c10e2590"
)

# Each client queries a CoreDNS interface on its own bridge: no cross-bridge
# routing, NAT, published ports or host networking.
NETWORKS = (
    ("lan", "198.51.100"),
    ("tailnet", "192.0.2"),
    ("other", "203.0.113"),
)
SERVER = "dns-smoke-coredns"
CONTROL_TIMEOUT = 120
DIG_TIMEOUT = 15
POLL_TIMEOUT = 30
POLL_INTERVAL = 1
TAILNET_ADDRESSES = {
    "@NAS-A_TAILNET_IPV4@": "192.0.2.41",
    "@NAS-B_TAILNET_IPV4@": "192.0.2.42",
    "@O11Y_TAILNET_IPV4@": "192.0.2.80",
}

TAILNET_ONLY_ANSWERS = {
    "ca.example.invalid": "192.0.2.44",
    "otlp.example.invalid": "192.0.2.80",
    "grafana.example.invalid": "192.0.2.80",
}


class SmokeError(Exception):
    """A setup or query failure whose details can be included in CI logs."""


class RunResult(Protocol):
    """Process execution result matching tbhbops_cli.o11y.runner.Result."""

    @property
    def code(self) -> int | None: ...

    @property
    def stdout(self) -> str: ...

    @property
    def stderr(self) -> str: ...


class CommandRunner(Protocol):
    """Process runner seam matching tbhbops_cli.o11y.runner.Runner."""

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
    ) -> RunResult: ...


class SmokeFilesystem(Protocol):
    """Filesystem seam matching the methods used on System.filesystem."""

    def temporary_directory(self) -> Path: ...
    def mkdir(self, path: Path) -> None: ...
    def chmod(self, path: Path, mode: int) -> None: ...
    def read_text(self, path: Path) -> str: ...
    def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None: ...


class SmokeSystem(Protocol):
    """Subset of tbhbops_cli.cli.System used by the DNS smoke command."""

    @property
    def env(self) -> Mapping[str, str]: ...

    @property
    def filesystem(self) -> SmokeFilesystem: ...

    @property
    def runner(self) -> CommandRunner: ...

    @property
    def monotonic(self) -> Callable[[], float]: ...

    @property
    def sleep(self) -> Callable[[float], None]: ...


def docker_environment(
    env: Mapping[str, str], dedicated_engine: str | None
) -> dict[str, str]:
    keys = (
        "PATH",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
    )
    result = {key: env[key] for key in keys if key in env}
    if dedicated_engine is not None:
        result["DOCKER_HOST"] = dedicated_engine
    return result


class Service(TypedDict):
    image: str
    command: list[str]
    cap_drop: list[str]
    cap_add: list[str]
    security_opt: list[str]
    read_only: bool
    mem_limit: str
    volumes: list[str]


def service_command(compose_text: str, staging: Path) -> tuple[str, list[str]]:
    """Read the image, command and hardening from the actual Compose service."""
    loaded: object = yaml.safe_load(compose_text)
    model: dict[str, dict[str, Service]] = cast(
        "dict[str, dict[str, Service]]",  # pragma: no mutate -- inert type argument
        loaded,
    )
    service = model["services"]["coredns"]
    image = service["image"]
    if re.fullmatch(r"[^@]+@sha256:[a-f0-9]{64}", image) is None:
        msg = "CoreDNS image must be digest-pinned"
        raise SmokeError(msg)
    command = [
        "create",
        "--name",
        SERVER,
        "--network",
        "dns-smoke-lan",
        "--ip",
        "198.51.100.2",
    ]
    for key, values in (
        ("cap-drop", service["cap_drop"]),
        ("cap-add", service["cap_add"]),
        ("security-opt", service["security_opt"]),
    ):
        for value in values:
            command.extend(["--" + key, value])
    if service["read_only"]:
        command.append("--read-only")
    command.extend(
        [
            "--memory",
            service["mem_limit"],
            "-e",
            "DNS_BIND_LAN=198.51.100.2",
            "-e",
            "DNS_BIND_TAILNET=192.0.2.2",
            "-e",
            "DNS_LAN_TAILNET_ADDRS=",
        ]
    )
    mount_dirs = {"DNS_CONFIG_DIR": "config", "DNS_ZONES_DIR": "zones"}
    for volume in service["volumes"]:
        setting, _, destination = volume.partition("}:")
        name = setting.removeprefix("${").partition(":")[0]
        if not destination or name not in mount_dirs:
            msg = "CoreDNS Compose mounts must use DNS_CONFIG_DIR and DNS_ZONES_DIR"
            raise SmokeError(msg)
        command.extend(["-v", f"{staging / mount_dirs[name]}:{destination}"])
    return image, [*command, image, *service["command"]]


def render_tailnet_zone(template: str) -> str:
    for placeholder, address in TAILNET_ADDRESSES.items():
        template = template.replace(placeholder, address)
    if re.search(r"^[^;\n]*@[A-Z0-9_]+@", template, re.MULTILINE):
        msg = "tailnet zone has unreplaced placeholders"
        raise SmokeError(msg)
    return template


def zone_records(text: str) -> dict[str, str]:
    """Read the project's one-relative-A-record-per-line zone convention."""
    records = {
        f"{name}.s3.example.invalid": address
        for name, address in re.findall(r"^(\S+) IN A (\S+)$", text, re.MULTILINE)
    }

    if not records:
        msg = "zone has no A records"
        raise SmokeError(msg)
    return records


@dataclass(frozen=True)
class Query:
    view: str
    prefix: str
    name: str
    statuses: frozenset[str]
    answers: frozenset[str]
    record_type: str = "A"


def tailnet_only_queries(view: str, prefix: str) -> list[Query]:
    return [
        Query(
            view,
            prefix,
            name,
            frozenset({"NOERROR" if view == "tailnet" else "REFUSED"}),
            frozenset({address})
            if view == "tailnet" and record_type == "A"
            else frozenset(),
            record_type,
        )
        for zone, address in TAILNET_ONLY_ANSWERS.items()
        for name in (zone, "ns." + zone)
        for record_type in ("A", "AAAA")
    ]


def s3_queries(view: str, prefix: str, records: dict[str, str]) -> list[Query]:
    return [
        Query(
            view,
            prefix,
            name,
            frozenset({"REFUSED" if view == "other" else "NOERROR"}),
            frozenset({address})
            if view != "other" and record_type == "A"
            else frozenset(),
            record_type,
        )
        for name, address in records.items()
        for record_type in ("A", "AAAA")
    ]


def queries(lan: str, tailnet: str) -> tuple[Query, ...]:
    checks: list[Query] = []
    for view, prefix in NETWORKS:
        records = zone_records(lan if view == "lan" else tailnet)
        checks.extend(s3_queries(view, prefix, records))
        checks.extend(tailnet_only_queries(view, prefix))
        checks.append(
            Query(
                view,
                prefix,
                "example.invalid",
                frozenset({"REFUSED"})
                if view == "other"
                else frozenset({"NOERROR", "NXDOMAIN", "SERVFAIL"}),
                frozenset(),
            )
        )
    return tuple(checks)


def dig_command(query: Query) -> list[str]:
    return [
        "run",
        "--rm",
        "--network",
        f"dns-smoke-{query.view}",
        "--ip",
        f"{query.prefix}.3",
        "--entrypoint",
        "dig",
        DIG_IMAGE,
        f"@{query.prefix}.2",
        query.name,
        query.record_type,
        "+time=2",
        "+tries=1",
        "+noall",
        "+comments",
        "+answer",
    ]


def answer_matches(query: Query, result: RunResult) -> bool:
    status = re.search(r"status: ([A-Z]+),", result.stdout)
    answers = frozenset(
        address
        for owner, address in re.findall(
            rf"^(\S+)\s+\d+\s+IN\s+{query.record_type}\s+(\S+)$",
            result.stdout,
            re.MULTILINE,
        )
        if owner.lower() == query.name.lower() + "."
    )
    return (
        result.code == 0
        and status is not None
        and status.group(1) in query.statuses
        and answers == query.answers
    )


@dataclass
class SmokeProbe:
    system: SmokeSystem
    env: dict[str, str]
    # Record only resources this run created; unwind them in reverse order.
    removals: list[list[str]] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def docker(self, command: list[str], timeout: int = CONTROL_TIMEOUT) -> RunResult:
        result = self.system.runner.run(["docker", *command], self.env, timeout)
        self.diagnostics.append(
            f"$ docker {' '.join(command)}\n{result.stdout}{result.stderr}\n"
            f"exit: {result.code}\n"
        )
        return result

    def require(self, command: list[str]) -> None:
        if self.docker(command).code != 0:
            msg = f"docker {' '.join(command)} failed"
            raise SmokeError(msg)

    def prepare(self, source: Path, staging: Path) -> tuple[str, list[str]]:
        fs = self.system.filesystem
        corefile = fs.read_text(source / "config/Corefile")
        # CoreDNS keeps each env value as one token. Add a third bind argument
        # only; retain every view expression and ACL verbatim.
        corefile = corefile.replace(
            "bind {$DNS_BIND_LAN} {$DNS_BIND_TAILNET}",
            "bind {$DNS_BIND_LAN} {$DNS_BIND_TAILNET} 203.0.113.2",
        )
        fs.mkdir(staging / "config")
        fs.mkdir(staging / "zones")
        fs.write_atomic(staging / "config/Corefile", corefile, 0o644)
        lan = fs.read_text(source / "zones/lan.zone")
        tailnet = render_tailnet_zone(fs.read_text(source / "zones/tailnet.zone.tmpl"))
        fs.write_atomic(staging / "zones/lan.zone", lan, 0o644)
        fs.write_atomic(staging / "zones/tailnet.zone", tailnet, 0o644)
        for name in TAILNET_ONLY_ANSWERS:
            zone = f"{name}.zone"
            source_name = zone if name == "ca.example.invalid" else zone + ".tmpl"
            rendered = render_tailnet_zone(fs.read_text(source / "zones" / source_name))
            fs.write_atomic(staging / "zones" / zone, rendered, 0o644)
        # The container drops DAC override: files are 0644 above, and every
        # directory it traverses must allow search by any uid.
        for directory in (staging, staging / "config", staging / "zones"):
            fs.chmod(directory, 0o755)  # nosec B103 -- container needs directory search
        return service_command(fs.read_text(source / "compose.yaml"), staging)

    def start(self, image: str, command: list[str]) -> None:
        # Pull before readiness polling so registry latency uses the control timeout.
        for reference in (image, DIG_IMAGE):
            self.require(["pull", reference])
        for view, prefix in NETWORKS:
            network = f"dns-smoke-{view}"
            self.require(
                [
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--subnet",
                    f"{prefix}.0/24",
                    network,
                ]
            )
            self.removals.append(["network", "rm", network])
        self.require(command)
        self.removals.append(["rm", "-f", SERVER])
        for view, prefix in NETWORKS[1:]:
            self.require(
                [
                    "network",
                    "connect",
                    "--ip",
                    f"{prefix}.2",
                    f"dns-smoke-{view}",
                    SERVER,
                ]
            )
        self.require(["start", SERVER])

    def check(self, checks: tuple[Query, ...], out: TextIO) -> None:
        deadline = self.system.monotonic() + POLL_TIMEOUT
        while not answer_matches(
            checks[0], self.docker(dig_command(checks[0]), DIG_TIMEOUT)
        ):
            if self.system.monotonic() >= deadline:
                msg = "CoreDNS readiness timed out"
                raise SmokeError(msg)
            self.system.sleep(POLL_INTERVAL)
        for query in checks:
            if not answer_matches(query, self.docker(dig_command(query), DIG_TIMEOUT)):
                msg = f"view {query.view}: {query.name} failed; see commands.log"
                raise SmokeError(msg)
            out.write(f"ok {query.view}: {query.name}\n")

    def cleanup(self) -> bool:
        results = [
            self.docker(command).code == 0 for command in reversed(self.removals)
        ]
        return all(results)


def dispatch(args: argparse.Namespace, system: SmokeSystem, out: TextIO) -> int:
    probe = SmokeProbe(system, docker_environment(system.env, args.dedicated_engine))
    status = 1
    try:
        staging = system.filesystem.temporary_directory()
        image, command = probe.prepare(args.source, staging)
        fs = system.filesystem
        checks = queries(
            fs.read_text(staging / "zones/lan.zone"),
            fs.read_text(staging / "zones/tailnet.zone"),
        )
        probe.start(image, command)
        probe.check(checks, out)
        status = 0
    except (
        SmokeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        yaml.YAMLError,
    ) as exc:
        out.write(f"FAIL dns smoke: {exc}\n")
    finally:
        probe.docker(["logs", SERVER])
        if not probe.cleanup():
            out.write("FAIL cleanup: smoke resources could not be removed\n")
            status = 1
        if status:
            system.filesystem.mkdir(args.artifacts)
            system.filesystem.write_atomic(
                args.artifacts / "commands.log", "".join(probe.diagnostics), 0o600
            )
    return status
