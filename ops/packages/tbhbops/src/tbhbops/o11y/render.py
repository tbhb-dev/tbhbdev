"""Describe and hash an observability release using plain data."""

import hashlib
import re
from dataclasses import dataclass
lazy from collections.abc import Mapping

REQUIRED = (
    "O11Y_DATA_DIR",
    "O11Y_GRAFANA_ROOT_URL",
    "O11Y_GRAFANA_OWNER_LOGIN",
)
OPTIONAL = (
    "O11Y_GRAFANA_PORT",
    "O11Y_OTLP_GRPC_PORT",
    "O11Y_OTLP_HTTP_PORT",
    "O11Y_SECRETS_OWNER",
    "O11Y_CLUSTER",
    "O11Y_HOST_NAME",
    "O11Y_ENVIRONMENT",
)
EMAIL = (
    "O11Y_ALERT_EMAIL_TO",
    "O11Y_ALERT_SMTP_HOST",
    "O11Y_ALERT_SMTP_PORT",
    "O11Y_ALERT_SMTP_USER",
    "O11Y_ALERT_SMTP_FROM",
    "O11Y_ALERT_SMTP_SKIP_VERIFY",
)
ADDRESS = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"
LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
NETWORK_INPUTS = {
    "O11Y_TS_HOSTNAME": r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)?",
    "O11Y_FIXED_ADDRESSES": r"(?:true|false)?",
    "O11Y_GRAFANA_TLS_CERT_DIR": r"(?:/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+)?",
}
EMAIL_RULES = (
    (EMAIL[0], ADDRESS, "must be one email address"),
    (
        EMAIL[1],
        rf"{LABEL}(\.{LABEL})*",
        "must be a host name, since O11Y_ALERT_EMAIL_TO is set",
    ),
    (
        EMAIL[2],
        r"[1-9][0-9]{0,4}",
        "must be a port from 1 to 65535, since O11Y_ALERT_EMAIL_TO is set",
    ),
    (
        EMAIL[3],
        r"[A-Za-z0-9._%+@-]+",
        (
            "must be a login of letters, digits and ._%+@-, since "
            "O11Y_ALERT_EMAIL_TO is set"
        ),
    ),
    (EMAIL[4], ADDRESS, "must be one email address, since O11Y_ALERT_EMAIL_TO is set"),
    (EMAIL[5], r"(true|false)", "must be true or false"),
)
MAX_PORT = 65535


@dataclass(frozen=True)
class Settings:
    values: tuple[tuple[str, str], ...]
    reserve_gb: int
    email: bool


@dataclass(frozen=True)
class Errors:
    messages: tuple[str, ...]


def _email_errors(env: Mapping[str, str]) -> tuple[str, ...]:
    errors = []
    for name, pattern, message in EMAIL_RULES:
        value = env.get(name, "")
        if name == EMAIL[5]:
            value = value or "false"
        if not re.fullmatch(pattern, value) or (
            name == EMAIL[2] and int(value) > MAX_PORT
        ):
            errors.append(f"{name} {message}")
    return tuple(errors)


def _values(env: Mapping[str, str], *, email: bool) -> tuple[tuple[str, str], ...]:
    values = [(name, env[name]) for name in REQUIRED]
    values.append(
        (
            "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD",
            env.get("O11Y_LOKI_WAL_DISK_FULL_THRESHOLD") or "0.90",
        )
    )
    values.extend((name, env[name]) for name in OPTIONAL if env.get(name))
    values.extend((name, env[name]) for name in NETWORK_INPUTS if env.get(name))
    if email:
        values.extend((name, env[name]) for name in EMAIL if env.get(name))
        values.append(("O11Y_ALERT_SMTP_ENABLED", "true"))
    return tuple(values)


def check(env: Mapping[str, str]) -> Settings | Errors:
    """Validate without including any supplied value in a diagnostic."""
    if "O11Y_POSTGRES_PORT" in env:
        return Errors(
            (
                (
                    "O11Y_POSTGRES_PORT is no longer supported; "
                    "PostgreSQL moved out of the stack"
                ),
            )
        )
    errors = tuple(
        f"{name} is not set" for name in REQUIRED if not env.get(name)
    ) or tuple(
        f"{name} has an invalid format"
        for name, pattern in NETWORK_INPUTS.items()
        if not re.fullmatch(pattern, env.get(name, ""))
    )
    if errors:
        return Errors(errors)
    if not re.fullmatch(ADDRESS, env[REQUIRED[2]]):
        return Errors(("O11Y_GRAFANA_OWNER_LOGIN must be one email address",))
    reserve = env.get("O11Y_DISK_RESERVE_GB") or "50"
    if not re.fullmatch(r"(0|[1-9][0-9]{0,5})", reserve):
        return Errors(
            (
                (
                    "O11Y_DISK_RESERVE_GB must be a whole number of GiB, "
                    "with no leading zero"
                ),
            )
        )
    email = bool(env.get(EMAIL[0]))
    if email:
        errors = _email_errors(env)
        if errors:
            return Errors(errors)
    return Settings(_values(env, email=email), int(reserve), email)


def substitute(text: str, settings: Settings) -> str:
    """Replace reserve tokens and select optional ranges as sed does."""
    text = text.replace("@O11Y_DISK_RESERVE_GB@", str(settings.reserve_gb))
    text = text.replace(
        "@O11Y_DISK_RESERVE_BYTES@", str(settings.reserve_gb * 1073741824)
    )
    values = dict(settings.values)
    for name, enabled in (
        ("O11Y_ALERT_EMAIL", settings.email),
        ("O11Y_TS_HOSTNAME", bool(values.get("O11Y_TS_HOSTNAME"))),
        ("O11Y_GRAFANA_TLS_CERT_DIR", bool(values.get("O11Y_GRAFANA_TLS_CERT_DIR"))),
        ("O11Y_FIXED_ADDRESSES", values.get("O11Y_FIXED_ADDRESSES") == "true"),
    ):
        text = _ranges(text, name, enabled=enabled)
    return text


def _ranges(text: str, name: str, *, enabled: bool) -> str:
    marker = f"# @{name}@ "
    prefix = marker + ("off " if enabled else "")
    end_marker = None
    kept: list[str] = []
    for line in re.findall(r"[^\n]*\n|[^\n]+", text):
        if end_marker is not None:
            if end_marker in line:
                end_marker = None
        elif prefix + "begin" in line:
            # A sed range never checks its end expression on the start line.
            end_marker = prefix + "end"
        elif marker not in line:
            kept.append(line)
    return "".join(kept)


def manifest(files: Mapping[str, bytes]) -> str:
    """Return C-locale sha256sum lines without the final newline."""
    return "\n".join(
        f"{hashlib.sha256(files[name]).hexdigest()}  ./{name}"
        for name in sorted(
            files, key=lambda name: name.encode(errors="surrogateescape")
        )
        if name.split("/")[-1] != "MANIFEST.sha256"
    )


def target_name(name: str, manifest_text: str) -> str:
    """Hash the manifest with the newline added by a bash here-string."""
    digest = hashlib.sha256(
        (manifest_text + "\n").encode(errors="surrogateescape")
    ).hexdigest()
    return f"{name}-{digest[:12]}"


def env_lines(
    settings: Settings, targets: Mapping[str, str], config_root: str
) -> tuple[str, ...]:
    """Return ordered deployment lines, before the differing S3 line."""
    return (
        *(f"{name}={value}" for name, value in settings.values),
        *(
            f"O11Y_CONFIG_{name.upper()}={config_root}/{targets[name]}"
            for name in sorted(
                targets, key=lambda name: name.encode(errors="surrogateescape")
            )
        ),
    )
