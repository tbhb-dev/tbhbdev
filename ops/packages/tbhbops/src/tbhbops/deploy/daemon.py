"""Verify Docker daemon capabilities and Compose resource ownership."""

from tbhbops.deploy.command import OUTPUT_LIMIT, Command, CommandError
from tbhbops.deploy.config import text
from tbhbops.deploy.protocol import Json, RefusalError, array, decode, mapping, require

# Docker info reports whether the daemon can enforce each Compose limit.
LIMIT_CAPABILITIES = {
    "pids_limit": "PidsLimit",
    "mem_limit": "MemoryLimit",
    "memswap_limit": "SwapLimit",
}


def capabilities(command: Command, candidate: Json) -> None:
    """Refuse reports that cannot prove the daemon enforces required limits."""
    try:
        report = mapping(
            decode(
                command(
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
        require(isinstance(value, str) and bool(value.strip()), "daemon unreachable")
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


def ownership(command: Command, project: str, model: Json) -> None:
    for kind in ("network", "volume"):
        for entry in mapping(mapping(model).get(kind + "s", {})).values():
            name = text(mapping(entry).get("name"))
            # Listing first distinguishes absence from an inspect failure.
            names = command(["/usr/bin/docker", kind, "ls", "--format", "{{.Name}}"])
            if name not in names.decode().splitlines():
                continue
            inspected = decode(command(["/usr/bin/docker", kind, "inspect", name]))
            require(
                isinstance(inspected, list) and len(inspected) == 1,
                "resource inspect type",
            )
            labels = mapping(mapping(array(inspected)[0]).get("Labels", {}))
            require(
                labels.get("com.docker.compose.project") == project,
                "foreign resource ownership",
            )
