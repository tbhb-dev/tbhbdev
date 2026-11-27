# cspell:ignore rustfsadmin
"""Describe preparation of the observability data directory without I/O."""

from dataclasses import dataclass
from string import ascii_letters, ascii_uppercase, digits
from typing import Literal


@dataclass(frozen=True)
class Secret:
    name: str
    charset: str
    length: int
    guard: str | None
    uid: int
    gid: int
    mode: int = 0o400


@dataclass(frozen=True)
class EnsureDir:
    name: str
    uid: int
    gid: int
    mode: int = 0o750


SECRETS = (
    Secret(
        "rustfs.access_key",
        ascii_uppercase + digits,
        20,
        "rustfs/.rustfs.sys",
        10001,
        10001,
    ),
    Secret(
        "rustfs.secret_key",
        ascii_letters + digits,
        40,
        "rustfs/.rustfs.sys",
        10001,
        10001,
    ),
    Secret(
        "grafana_admin_password",
        ascii_letters + digits,
        32,
        "grafana/grafana.db",
        472,
        472,
    ),
    Secret(
        "grafana_agents_password",
        ascii_letters + digits,
        32,
        None,
        472,
        472,
    ),
)
DIRECTORIES = (
    EnsureDir("rustfs", 10001, 10001),
    EnsureDir("loki", 10001, 10001),
    EnsureDir("tempo", 10001, 10001),
    EnsureDir("mimir", 0, 0),
    EnsureDir("alloy", 0, 0),
    EnsureDir("grafana", 472, 472),
    EnsureDir("secrets", 0, 0, 0o755),
)
WEBHOOK = "alert_discord_webhook_url"
SMTP = "alert_smtp_password"
PLACEHOLDER = "http://127.0.0.1:9/o11y-no-webhook"
type Status = Literal["missing", "invalid", "ok", "unreadable"]


@dataclass(frozen=True)
class Facts:
    statuses: tuple[Status, ...]
    guards: tuple[bool, ...]
    webhook_empty: bool
    smtp_exists: bool
    leftovers: tuple[str, ...]
    symlinks: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemoveLeftover:
    name: str


@dataclass(frozen=True)
class GenerateSecret:
    secret: Secret
    invalid: bool = False


@dataclass(frozen=True)
class WritePlaceholder:
    name: str


@dataclass(frozen=True)
class WriteS3Env:
    uid: int
    gid: int


@dataclass(frozen=True)
class SetOwnerMode:
    name: str
    uid: int
    gid: int
    mode: int


type Step = (
    EnsureDir
    | RemoveLeftover
    | GenerateSecret
    | WritePlaceholder
    | WriteS3Env
    | SetOwnerMode
)


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Refusal:
    message: str


@dataclass(frozen=True)
class Incomplete(Refusal):
    """Inspection needs more access before planning can finish."""


def valid(value: str, charset: str, length: int) -> bool:
    """Accept exactly LENGTH allowed characters, excluding the default login."""
    return (
        len(value) == length
        and all(c in charset for c in value)
        and value != "rustfsadmin"
    )


def plan(facts: Facts, secrets_owner: tuple[int, int]) -> Plan | Refusal:
    """Refuse unsafe facts before describing any changes."""
    if facts.symlinks:
        return Refusal(f"refusing symlink {facts.symlinks[0]!r}; nothing was changed")
    generations = []
    for secret, status, guard in zip(
        SECRETS, facts.statuses, facts.guards, strict=True
    ):
        if status == "unreadable":
            return Incomplete(f"can't read {secret.name} without root; plan incomplete")
        if status != "ok" and guard:
            return Refusal(
                f"{secret.name} is missing or damaged and "
                f"{secret.guard} already exists; "
                "restore the file, nothing was changed"
            )
        if status != "ok":
            generations.append(GenerateSecret(secret, invalid=status == "invalid"))
    steps: list[Step] = list(DIRECTORIES)
    steps.extend(RemoveLeftover(name) for name in facts.leftovers)
    steps.extend(generations)
    if facts.webhook_empty:
        steps.append(WritePlaceholder(WEBHOOK))
    if not facts.smtp_exists:
        steps.append(WritePlaceholder(SMTP))
    steps.append(WriteS3Env(*secrets_owner))
    steps.extend(
        SetOwnerMode(f"secrets/{s.name}", s.uid, s.gid, s.mode) for s in SECRETS
    )
    steps.extend(
        SetOwnerMode(f"secrets/{name}", 472, 472, 0o400) for name in (WEBHOOK, SMTP)
    )
    return Plan(tuple(steps))
