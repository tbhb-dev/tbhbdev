"""The root-owned receiver and service allow-list schemas."""

import ipaddress
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.filesystem import spelling
from tbhbops.deploy.protocol import Json, mapping, relative, require, strings

MAX_PORT = 65535
MAX_MODE = 0o777
MODE_LIMIT = MAX_MODE + 1
REF_COMPONENTS = 3
SERVICE = re.compile(r"[a-z][a-z0-9-]{0,31}")
NAME = re.compile(r"[A-Za-z0-9_.-]+")


def text(value: Json) -> str:
    require(isinstance(value, str), "expected string")
    return cast(
        # typing.cast ignores its type argument at runtime.
        "str",  # pragma: no mutate
        value,
    )


def integer(value: Json, minimum: int = 0) -> int:
    require(type(value) is int, "expected integer")
    result = cast(
        # typing.cast ignores its type argument at runtime.
        "int",  # pragma: no mutate
        value,
    )
    require(result >= minimum, "integer below minimum")
    return result


def rows(value: Json) -> list[dict[str, Json]]:
    require(isinstance(value, list), "expected table list")
    return [
        mapping(item)
        for item in cast(
            # typing.cast ignores its type argument at runtime.
            "list[Json]",  # pragma: no mutate
            value,
        )
    ]


@dataclass(frozen=True)
class Secret:
    name: str
    op: str
    target: Path
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class Service:
    name: str
    root: Path
    compose_file: str
    compose_env_file: str
    min_free_gib: int
    max_stream_mib: int
    init: tuple[str, ...]
    runc_exceptions: tuple[str, ...]
    timeout_s: int
    health_services: tuple[str, ...]
    probes: tuple[tuple[str, tuple[str, ...]], ...]
    ports: tuple[tuple[str, int, str], ...]
    networks: dict[str, str]
    secrets: tuple[Secret, ...]
    images: tuple[str, ...]
    ro_binds: tuple[Path, ...]
    max_retained_mib: int = 512


def repository(value: str) -> str:
    host, separator, _ = value.partition("/")
    require(
        bool(separator) and ("." in host or ":" in host or host == "localhost"),
        "image registry host",
    )
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9.:-]*/[a-z0-9]+(?:[._/-][a-z0-9]+)*", value)
        is not None,
        "image repository spelling",
    )
    return value


def ro_bind(value: str, root: Path) -> Path:
    path = spelling(value)
    require(root in path.parents, "read-only bind outside root")
    require(
        path.relative_to(root).parts[0] not in {"deploy", "incoming", "releases"},
        "read-only bind control path",
    )
    return path


def secret(row: dict[str, Json], root: Path) -> Secret:
    mapping(row, {"name", "op", "target", "mode", "uid", "gid"})
    require(set(row) == {"name", "op", "target", "mode", "uid", "gid"}, "secret keys")
    name, op, target = (text(row[key]) for key in ("name", "op", "target"))
    require(NAME.fullmatch(name) is not None, "secret name")
    require(name != "metadata.json", "reserved secret name")
    parts = op.removeprefix("op://").split("/")
    require(
        op.startswith("op://") and len(parts) == REF_COMPONENTS and all(parts),
        "secret reference",
    )
    require(relative(target) == target and "/" in target, "secret target")
    require(
        target.split("/")[0] not in {"deploy", "incoming", "releases"},
        "secret control target",
    )
    mode, uid, gid = (integer(row[key]) for key in ("mode", "uid", "gid"))
    require(mode < MODE_LIMIT and mode & 0o022 == 0, "secret mode")
    return Secret(name, op, root / target, mode, uid, gid)


def service_ports(value: Json) -> list[tuple[str, int, str]]:
    ports = []
    for row in rows(value):
        require(set(row) == {"host_ip", "port", "protocol"}, "port keys")
        host, protocol = text(row["host_ip"]), text(row["protocol"])
        address = ipaddress.ip_address(host)
        require(not address.is_unspecified, "unspecified port address")
        port = integer(row["port"], 1)
        require(
            port <= MAX_PORT and protocol in {"tcp", "udp"}, "port range or protocol"
        )
        ports.append((host, port, protocol))
    return ports


def service_networks(value: Json) -> dict[str, str]:
    networks: dict[str, str] = {}
    for row in rows(value):
        require(set(row) == {"key", "subnet"}, "network keys")
        key, subnet = text(row["key"]), text(row["subnet"])
        require(key not in networks, "duplicate network")
        ipaddress.ip_network(subnet)
        networks[key] = subnet
    return networks


def service(value: Json, name: str) -> Service:
    keys = {
        "format",
        "service",
        "root",
        "compose_file",
        "compose_env_file",
        "min_free_gib",
        "max_stream_mib",
        "max_retained_mib",
        "init",
        "runc_exceptions",
        "health",
        "ports",
        "networks",
        "secrets",
        "images",
        "ro_binds",
    }
    model = mapping(value, keys)
    require(
        keys - {"init", "networks", "max_retained_mib"} <= model.keys(),
        "missing service key",
    )
    require(
        SERVICE.fullmatch(name) is not None and model["service"] == name, "service name"
    )
    require(integer(model["format"]) == 1, "service format")
    root = Path(text(model["root"]))
    require(str(root) == f"/srv/{name}", "service root")
    compose, env = (text(model[key]) for key in ("compose_file", "compose_env_file"))
    require(relative(compose) == compose and relative(env) == env, "compose path")
    init = strings(model.get("init", []))
    require(not init or Path(init[0]).is_absolute(), "init executable")
    if init:
        executable = spelling(init[0])
        require(
            executable != root and root not in executable.parents,
            "init inside service root",
        )
    exceptions = strings(model["runc_exceptions"])
    health = mapping(model["health"], {"timeout_s", "services", "probes"})
    require(set(health) == {"timeout_s", "services", "probes"}, "health keys")
    services = strings(health["services"])
    require(bool(services) and len(set(services)) == len(services), "health services")
    probes = []
    for row in rows(health["probes"]):
        require(set(row) == {"service", "argv"}, "probe keys")
        target, argv = text(row["service"]), strings(row["argv"])
        require(target in services and bool(argv), "probe target or arguments")
        probes.append((target, tuple(argv)))
    ports = service_ports(model["ports"])
    networks = service_networks(model.get("networks", []))
    secrets = tuple(secret(row, root) for row in rows(model["secrets"]))
    require(len({s.name for s in secrets}) == len(secrets), "duplicate secret name")
    require(len({s.target for s in secrets}) == len(secrets), "duplicate secret target")
    return Service(
        name,
        root,
        compose,
        env,
        integer(model["min_free_gib"]),
        integer(model["max_stream_mib"], 1),
        tuple(init),
        tuple(exceptions),
        integer(health["timeout_s"], 1),
        tuple(services),
        tuple(probes),
        tuple(ports),
        networks,
        secrets,
        tuple(repository(image) for image in strings(model["images"])),
        tuple(ro_bind(path, root) for path in strings(model["ro_binds"])),
        integer(model.get("max_retained_mib", 512), 1),
    )


def load(path: Path) -> Json:
    return cast(
        # typing.cast ignores its type argument at runtime.
        "Json",  # pragma: no mutate
        tomllib.loads(fs.control_read(path).decode()),
    )


def load_sender(path: Path) -> Json:
    """Read sender configuration from the invoking user's checkout."""
    return tomllib.loads(path.read_text())
