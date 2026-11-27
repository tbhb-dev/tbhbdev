"""Fail-closed schemas for the normalized Compose v5.7.1 model."""

import ipaddress
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import cast
lazy from pathlib import Path

from tbhbops.deploy.config import MAX_PORT, Service, integer, repository, rows, text
from tbhbops.deploy.filesystem import spelling
from tbhbops.deploy.protocol import Json, mapping, require, strings

SERVICE_KEYS = {
    "image",
    "command",
    "entrypoint",
    "environment",
    "env_file",
    "user",
    "working_dir",
    "volumes",
    "tmpfs",
    "ports",
    "expose",
    "networks",
    "depends_on",
    "healthcheck",
    "restart",
    "init",
    "read_only",
    "cap_drop",
    "security_opt",
    "mem_limit",
    "memswap_limit",
    "cpus",
    "pids_limit",
    "ulimits",
    "stop_grace_period",
    "stop_signal",
    "labels",
    "logging",
    "extra_hosts",
    "secrets",
    "configs",
    "runtime",
    "deploy",
}
STRING_KEYS = {
    "image",
    "user",
    "working_dir",
    "restart",
    "mem_limit",
    "memswap_limit",
    "stop_grace_period",
    "stop_signal",
    "runtime",
}
MAX_STOP_GRACE_S = 60
MAX_MEMORY_BYTES = 64 * 1024**3
MAX_LOG_SIZE_BYTES = 100 * 1024**2
DURATION_UNITS = {
    "h": 3600_000_000_000,
    "m": 60_000_000_000,
    "s": 1_000_000_000,
    "ms": 1_000_000,
    "us": 1_000,
    "ns": 1,
}
DURATION_PART = r"([0-9]+(?:\.[0-9]+)?)(ms|us|ns|h|m|s)"

LIST_KEYS = {"tmpfs", "expose", "cap_drop", "security_opt", "extra_hosts"}
HEALTH_KEYS = {
    "test",
    "interval",
    "timeout",
    "retries",
    "start_period",
    "start_interval",
    "disable",
}


def typed(value: Json, expected: type) -> None:
    require(type(value) is expected, "wrong value type")


def string_map(value: Json, *, nullable: bool = False, labels: bool = False) -> None:
    for key, item in mapping(value).items():
        require(
            isinstance(item, str) or (nullable and item is None), "mapping value type"
        )
        require(
            not labels or not key.startswith("com.docker.compose."), "reserved label"
        )


@dataclass(frozen=True)
class Source:
    path: Path
    writable: bool = False
    file: bool = False
    env: bool = False


def _volume(value: Json, named: dict[str, Json]) -> Source | None:
    volume = mapping(
        value, {"type", "source", "target", "read_only", "bind", "volume", "tmpfs"}
    )
    kind = text(volume.get("type"))
    require(kind in {"bind", "volume", "tmpfs"}, "mount type")
    text(volume.get("target"))
    if "read_only" in volume:
        typed(volume["read_only"], bool)
    for key, fields in (
        ("bind", {"create_host_path"}),
        ("volume", {"nocopy"}),
        ("tmpfs", {"size", "mode"}),
    ):
        if key in volume:
            require(key == kind, "mount option type")
            for item, setting in mapping(volume[key], fields).items():
                typed(setting, int if item in {"size", "mode"} else bool)
    if kind == "bind":
        return Source(
            spelling(text(volume.get("source"))),
            not bool(volume.get("read_only")),
        )
    if kind == "volume":
        require(text(volume.get("source")) in named, "foreign volume")
    else:
        require("source" not in volume, "tmpfs source")
    return None


def _ports(value: Json, allowed: Service) -> None:
    for port in rows(value):
        mapping(port, {"mode", "host_ip", "target", "published", "protocol"})
        require(port.get("mode", "ingress") in ("ingress", "host"), "port mode")
        target = integer(port.get("target"), 1)
        require(target <= MAX_PORT, "target port")
        published = text(port.get("published"))
        require(published.isascii() and published.isdecimal(), "published port")
        key = (text(port.get("host_ip")), int(published), text(port.get("protocol")))
        require(key in allowed.ports, "unlisted port")


def _networks(value: Json, networks: dict[str, Json], allowed: Service) -> None:
    for key, entry in mapping(value).items():
        require(key in networks, "unknown network")
        if entry is None:
            continue
        options = mapping(entry, {"aliases", "ipv4_address"})
        if "aliases" in options:
            strings(options["aliases"])
        if "ipv4_address" in options:
            require(key in allowed.networks, "unlisted subnet")
            address = ipaddress.ip_address(text(options["ipv4_address"]))
            require(
                address in ipaddress.ip_network(allowed.networks[key]),
                "address outside subnet",
            )


def _health(value: Json) -> None:
    for key, entry in mapping(value, HEALTH_KEYS).items():
        if key == "test":
            strings(entry)
        elif key == "retries":
            integer(entry)
        elif key == "disable":
            typed(entry, bool)
        else:
            text(entry)


def _dependencies(value: Json, services: dict[str, Json]) -> None:
    for key, entry in mapping(value).items():
        require(key in services, "unknown dependency")
        for option, setting in mapping(
            entry, {"condition", "required", "restart"}
        ).items():
            if option == "condition":
                text(setting)
            else:
                typed(setting, bool)


def _resources(value: Json) -> None:
    model = mapping(value, {"resources"})
    for kind, entry in mapping(
        model.get("resources", {}), {"limits", "reservations"}
    ).items():
        fields = {"cpus", "memory", "pids"} if kind == "limits" else {"cpus", "memory"}
        for key, setting in mapping(entry, fields).items():
            if key == "pids":
                integer(setting)
            else:
                text(setting)


def _ulimits(value: Json) -> None:
    for entry in mapping(value, {"nofile", "nproc"}).values():
        if type(entry) is int:
            continue
        for setting in mapping(entry, {"soft", "hard"}).values():
            typed(setting, int)


def _references(value: Json, top: Json) -> None:
    names = mapping(top)
    for entry in rows(value):
        mapping(entry, {"source", "target", "uid", "gid", "mode"})
        require(text(entry.get("source")) in names, "unknown file reference")
        for key, item in entry.items():
            if key == "mode":
                integer(item)
            else:
                text(item)


def _stop_grace(value: Json) -> None:
    duration = text(value)
    require(
        re.fullmatch(f"(?:{DURATION_PART})+", duration) is not None,
        "stop grace duration",
    )
    nanoseconds = sum(
        Decimal(amount) * DURATION_UNITS[unit]
        for amount, unit in re.findall(DURATION_PART, duration)
    )
    require(nanoseconds <= MAX_STOP_GRACE_S * 1_000_000_000, "stop grace ceiling")


def _plain(key: str, item: Json) -> None:
    if key == "stop_grace_period":
        _stop_grace(item)
    elif key in STRING_KEYS:
        text(item)
    elif key in LIST_KEYS:
        strings(item)
    elif key in {"command", "entrypoint"}:
        if item is not None:
            strings(item)
    elif key in {"init", "read_only"}:
        typed(item, bool)
    elif key == "pids_limit":
        integer(item, 1)
    elif key == "cpus":
        require(type(item) in {int, float}, "cpu type")
    else:  # All remaining plain keys are environment and labels.
        string_map(item, nullable=key == "environment", labels=key == "labels")


def _mounts(key: str, value: Json, model: dict[str, Json]) -> list[Source]:
    paths: list[Source] = []
    for entry in rows(value):
        if key == "env_file":
            mapping(entry, {"path", "required"})
            require(entry.get("required", True) is True, "optional env file")
            paths.append(Source(spelling(text(entry.get("path"))), file=True, env=True))
        else:
            source = _volume(entry, mapping(model.get("volumes", {})))
            if source is not None:
                paths.append(source)
    return paths


def _positive_size(value: Json, ceiling: int) -> None:
    size = text(value)
    require(
        re.fullmatch(r"0*[1-9][0-9]*[bkmgBKMG]?", size) is not None,
        "positive size required",
    )

    multiplier = {"k": 1024, "m": 1024**2, "g": 1024**3}.get(size[-1].lower(), 1)
    require(int(size.rstrip("bkmgBKMG")) * multiplier <= ceiling, "size ceiling")  # pragma: no mutate -- tech debt: surviving mutant x__positive_size__mutmut_47, decision no-mutation-on-prs  # fmt: skip


def _logging(value: Json) -> None:
    options = mapping(value, {"driver", "options"})
    require(options.get("driver") == "json-file", "logging driver")
    settings = mapping(options.get("options", {}))
    require(
        not any(
            key.lower().replace("_", "-") in {"env", "env-regex", "labels-regex"}
            for key in settings
        ),
        "unsafe logging option",
    )
    string_map(settings)
    _positive_size(settings.get("max-size"), MAX_LOG_SIZE_BYTES)
    require(
        re.fullmatch(r"0*[1-9][0-9]*", text(settings.get("max-file"))) is not None,
        "positive log file count required",
    )


def _limits(service: dict[str, Json]) -> None:
    memory = service.get("mem_limit")
    deploy = mapping(service.get("deploy", {}))
    resources = mapping(deploy.get("resources", {}))
    limits = mapping(resources.get("limits", {}))
    # Reject an explicit unlimited limit even when the alternate spelling is bounded.
    if memory is not None:
        _positive_size(memory, MAX_MEMORY_BYTES)
    if "memory" in limits:
        _positive_size(limits["memory"], MAX_MEMORY_BYTES)
    if "memswap_limit" in service:
        _positive_size(service["memswap_limit"], MAX_MEMORY_BYTES)
    require(memory is not None or "memory" in limits, "memory limit required")
    integer(service.get("pids_limit"), 1)
    _logging(service.get("logging"))


def _structured(key: str, item: Json, model: dict[str, Json], allowed: Service) -> None:
    validators = {
        "healthcheck": _health,
        "logging": _logging,
        "deploy": _resources,
        "ulimits": _ulimits,
    }
    if key in validators:
        validators[key](item)
    elif key == "ports":
        _ports(item, allowed)
    elif key == "networks":
        _networks(item, mapping(model.get("networks", {})), allowed)
    elif key == "depends_on":
        _dependencies(item, mapping(model["services"]))
    elif key in {"secrets", "configs"}:
        _references(item, model.get(key, {}))
    else:
        _plain(key, item)


def _service(
    name: str, value: Json, model: dict[str, Json], allowed: Service
) -> list[Source]:
    service = mapping(value, SERVICE_KEYS)
    image = re.fullmatch(
        r"([^@]+?)(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?@sha256:[0-9a-f]{64}",
        text(service.get("image")),
    )
    require(image is not None, "image digest")
    repo = cast(
        # typing.cast ignores its type argument at runtime.
        "re.Match[str]",  # pragma: no mutate
        image,
    ).group(1)
    require(repository(repo) in allowed.images, "unlisted image repository")
    paths: list[Source] = []
    for key, item in service.items():
        if key in {"env_file", "volumes"}:
            paths.extend(_mounts(key, item, model))
        else:
            _structured(key, item, model, allowed)
    _limits(service)
    runtime = service.get("runtime")
    require(
        runtime == "runsc" or (runtime == "runc" and name in allowed.runc_exceptions),
        "runtime",
    )
    require(
        set(strings(service.get("security_opt", []))) <= {"no-new-privileges:true"},
        "security option",
    )
    for host in strings(service.get("extra_hosts", [])):
        require("=" in host and bool(host.partition("=")[0]), "extra host")
        ipaddress.ip_address(host.split("=", 1)[1])
    return paths


def _network(key: str, network: dict[str, Json], allowed: Service) -> None:
    require(network.get("driver") in (None, "bridge"), "network driver")
    if "internal" in network:
        typed(network["internal"], bool)
    ipam = mapping(network.get("ipam", {}), {"config"})
    if ipam:
        expected: Json = [{"subnet": allowed.networks.get(key)}]
        require(
            key in allowed.networks and ipam["config"] == expected, "network subnet"
        )


def _named(model: dict[str, Json], allowed: Service) -> None:
    for kind in ("networks", "volumes"):
        for key, entry in mapping(model.get(kind, {})).items():
            fields = (
                {"name", "labels", "driver", "internal", "ipam"}
                if kind == "networks"
                else {"name", "labels"}
            )
            resource = mapping(entry, fields)
            require(resource.get("name") == f"{allowed.name}_{key}", "physical name")
            string_map(resource.get("labels", {}), labels=True)
            if kind == "networks":
                _network(key, resource, allowed)


def model(value: Json, allowed: Service) -> tuple[Source, ...]:
    result = mapping(value)
    require(
        all(
            key in {"name", "services", "networks", "volumes", "secrets", "configs"}
            or key.startswith("x-")
            for key in result
        ),
        "top-level key",
    )
    require(result.get("name") == allowed.name, "project name")
    services = mapping(result.get("services"))
    require(bool(services), "empty services")
    paths = []
    for name, entry in services.items():
        paths.extend(_service(name, entry, result, allowed))
    _named(result, allowed)
    for kind in ("secrets", "configs"):
        for entry in mapping(result.get(kind, {})).values():
            file = mapping(entry, {"name", "file"})
            if "name" in file:
                text(file["name"])
            paths.append(Source(spelling(text(file.get("file"))), file=True))
    return tuple(paths)


def retained(value: Json) -> tuple[Source, ...]:
    """Read host paths from a model validated by its original policy."""
    result = mapping(value)
    paths: list[Source] = []
    for entry in mapping(result.get("services")).values():
        service = mapping(entry)
        paths.extend(
            Source(spelling(text(row.get("path"))), file=True, env=True)
            for row in rows(service.get("env_file", []))
        )
        paths.extend(
            Source(spelling(text(row.get("source"))), not bool(row.get("read_only")))
            for row in rows(service.get("volumes", []))
            if row.get("type") == "bind"
        )
    for kind in ("secrets", "configs"):
        paths.extend(
            Source(spelling(text(mapping(entry).get("file"))), file=True)
            for entry in mapping(result.get(kind, {})).values()
        )
    return tuple(paths)


def paths(
    sources: tuple[Source, ...],
    retained: tuple[Source, ...],
    writable: set[Path],
    allowed: Service,
    release: str,
) -> None:
    """Check lexical and cross-release rules before descriptor-based inspection."""
    root = allowed.root
    own = root / "releases" / release
    secrets = {secret.target for secret in allowed.secrets}
    controls = {
        root / "deploy",
        root / "incoming",
        root / "releases",
        *secrets,
        *(path.parent for path in secrets),
        *allowed.ro_binds,
        *(path.parent for path in allowed.ro_binds),
    }
    # current and previous under root are symlinks into releases/. ro_binds
    # excludes deploy, incoming and releases, but not those two aliases. Docker
    # resolves bind sources; our descriptor mount check refuses symlinks before
    # an alias can bypass the release rules above.
    candidate_writes = {source.path for source in sources if source.writable}
    for source in sources:
        path = source.path
        require(
            re.fullmatch(r"/[A-Za-z0-9._/-]+", str(path)) is not None, "path characters"
        )
        require(
            root in path.parents
            and all(part not in {".", ".."} for part in path.parts),
            "path outside root",
        )
        require(
            not any(
                path.is_relative_to(entry) and not (source.writable and path == entry)
                for entry in writable
            ),
            "path under writable bind",
        )
        if source.writable:
            require(
                not any(
                    path.is_relative_to(control) or control.is_relative_to(path)
                    for control in controls
                ),
                "writable control path",
            )
            require(
                not any(
                    old.path.is_relative_to(path)
                    and not (old.writable and old.path == path)
                    for old in retained
                ),
                "writable retained path",
            )
        else:
            if source.env:
                require(
                    path in secrets or path.is_relative_to(own), "unlisted env file"
                )
            require(
                not path.is_relative_to(root / "deploy")
                and not path.is_relative_to(root / "incoming"),
                "read-only control path",
            )
            require(
                not path.is_relative_to(root / "releases") or path.is_relative_to(own),
                "foreign release path",
            )
            require(
                path.is_relative_to(own) or path in secrets or path in allowed.ro_binds,
                "unlisted read-only source",
            )
    require(candidate_writes <= writable, "incomplete writable set")
