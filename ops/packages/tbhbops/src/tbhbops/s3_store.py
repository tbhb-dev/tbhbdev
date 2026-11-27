"""Pure construction of SeaweedFS S3 identities with bucket-scoped access."""

import re
from dataclasses import dataclass, field
from typing import TypedDict

O11Y_BUCKETS = ("loki", "mimir-blocks", "mimir-ruler", "tempo")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_ADDRESS = re.compile(r"[0-9]+(?:\.[0-9]+){3}")


@dataclass(frozen=True)
class Keys:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


@dataclass(frozen=True)
class Consumer:
    name: str
    buckets: tuple[str, ...]
    keys: Keys


class Credential(TypedDict):
    accessKey: str
    secretKey: str


class Identity(TypedDict):
    name: str
    credentials: list[Credential]
    actions: list[str]


class Document(TypedDict):
    identities: list[Identity]


def validate_bucket(name: str) -> None:
    """Accept general purpose S3 bucket names, without address-shaped names."""
    if (
        _BUCKET.fullmatch(name) is None
        or any(part in name for part in ("..", ".-", "-."))
        or _ADDRESS.fullmatch(name)
        or name.startswith(("xn--", "sthree-", "amzn-s3-demo-"))
        or name.endswith(("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3"))
    ):
        message = "invalid S3 bucket name"
        raise ValueError(message)


def _identity(name: str, keys: Keys, actions: list[str]) -> Identity:
    if not keys.access_key.strip() or not keys.secret_key.strip():
        message = "S3 access and secret keys must be nonempty"
        raise ValueError(message)
    return {
        "name": name,
        "credentials": [{"accessKey": keys.access_key, "secretKey": keys.secret_key}],
        "actions": actions,
    }


def build_identities(admin: Keys, consumers: tuple[Consumer, ...]) -> Document:
    """Grant the administrator Admin and consumers only their named buckets."""
    identities = [_identity("admin", admin, ["Admin"])]
    names = {"admin", "anonymous"}
    access_keys = {admin.access_key}
    for consumer in consumers:
        if not consumer.name.strip() or consumer.name in names:
            message = "consumer names must be nonempty, unique and unreserved"
            raise ValueError(message)
        names.add(consumer.name)
        if consumer.keys.access_key in access_keys:
            message = "access keys must be unique across identities"
            raise ValueError(message)
        access_keys.add(consumer.keys.access_key)
        if not consumer.buckets or len(set(consumer.buckets)) != len(consumer.buckets):
            message = "each consumer needs a nonempty list of unique buckets"
            raise ValueError(message)
        actions = []
        for bucket in consumer.buckets:
            validate_bucket(bucket)
            actions.extend(f"{action}:{bucket}" for action in ("Read", "Write", "List"))
        identities.append(_identity(consumer.name, consumer.keys, actions))
    return {"identities": identities}
