"""Validate normalized Compose values and required resource bounds."""

import re
from decimal import Decimal

from tbhbops.deploy.config import integer, text
from tbhbops.deploy.protocol import Json, mapping, require, strings

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


def health(value: Json) -> None:
    for key, entry in mapping(value, HEALTH_KEYS).items():
        if key == "test":
            strings(entry)
        elif key == "retries":
            integer(entry)
        elif key == "disable":
            typed(entry, bool)
        else:
            text(entry)


def resources(value: Json) -> None:
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


def ulimits(value: Json) -> None:
    for entry in mapping(value, {"nofile", "nproc"}).values():
        if type(entry) is int:
            continue
        for setting in mapping(entry, {"soft", "hard"}).values():
            typed(setting, int)


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


def plain(key: str, item: Json) -> None:
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


def _positive_size(value: Json, ceiling: int) -> None:
    size = text(value)
    require(
        re.fullmatch(r"0*[1-9][0-9]*[bkmgBKMG]?", size) is not None,
        "positive size required",
    )

    multiplier = {"k": 1024, "m": 1024**2, "g": 1024**3}.get(size[-1].lower(), 1)
    require(int(size.rstrip("bkmgBKMG")) * multiplier <= ceiling, "size ceiling")  # pragma: no mutate -- tech debt: surviving mutant x__positive_size__mutmut_47, decision no-mutation-on-prs  # fmt: skip


def logging(value: Json) -> None:
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


def required_limits(service: dict[str, Json]) -> None:
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
    require("pids_limit" in service, "expected integer")
    require("logging" in service, "expected an object")
