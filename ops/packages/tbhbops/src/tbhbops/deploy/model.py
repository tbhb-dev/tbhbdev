"""Validate normalized Compose projects against their service allow-list."""

import ipaddress
import re
from typing import cast

from tbhbops.deploy import model_fields, mounts
from tbhbops.deploy.config import MAX_PORT, Service, integer, repository, rows, text
from tbhbops.deploy.model_fields import string_map, typed
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


def _structured(key: str, item: Json, model: dict[str, Json], allowed: Service) -> None:
    validators = {
        "healthcheck": model_fields.health,
        "logging": model_fields.logging,
        "deploy": model_fields.resources,
        "ulimits": model_fields.ulimits,
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
        model_fields.plain(key, item)


def _service(
    name: str, value: Json, model: dict[str, Json], allowed: Service
) -> list[mounts.Source]:
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
    paths: list[mounts.Source] = []
    for key, item in service.items():
        if key in {"env_file", "volumes"}:
            paths.extend(mounts.service_sources(key, item, model))
        else:
            _structured(key, item, model, allowed)
    model_fields.required_limits(service)
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


def validate(value: Json, allowed: Service) -> tuple[mounts.Source, ...]:
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
    paths.extend(mounts.file_sources(result))
    return tuple(paths)
