# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Hash session IDs and read values from identity records."""

from __future__ import annotations

import hashlib
import json
import re
from typing import cast
lazy from collections.abc import Mapping

HASH_RE = re.compile(r"sha-256-64:[0-9a-f]{16}")


type Json = dict[str, object]


def sha256_64(raw: object) -> str:
    """Hash a raw ID to ``sha-256-64:`` plus the first 16 hex of its SHA-256."""
    return "sha-256-64:" + hashlib.sha256(str(raw).encode()).hexdigest()[:16]


def hx(hashed: str) -> str:
    """The hex part of a ``sha-256-64:`` hash."""
    return hashed.split(":")[1]


def as_str(value: object) -> str | None:
    """The value if it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def as_dict(value: object) -> Json | None:
    """The value if it is a JSON object, else None."""
    if isinstance(value, dict):
        return cast("Json", value)  # pragma: no mutate -- a runtime no-op
    return None


def first_str(data: Json, *names: str) -> str | None:
    """The first of ``names`` whose value in ``data`` is a non-empty string."""
    for name in names:
        value = as_str(data.get(name))
        if value:
            return value
    return None


def hash_ok(value: object) -> str | None:
    """A ``sha-256-64:`` hash, or None for anything else."""
    return value if isinstance(value, str) and HASH_RE.fullmatch(value) else None


def parse_line(line: str) -> Json | None:
    """One JSON-lines record, or None if it is not a JSON object."""
    try:
        return as_dict(json.loads(line))
    except ValueError:
        return None


def merge_records(records: list[Json]) -> Json:
    """Prefer the later store for each field and the latest switch as a pair."""
    merged: Json = {}
    for rec in records:
        merged.update(rec)
    switches = [
        (at, rec.get("switch_model"))
        for rec in records
        if isinstance(at := rec.get("switch_at"), float)
    ]
    if switches:
        merged["switch_at"], merged["switch_model"] = max(switches, key=lambda s: s[0])
    return merged


def rec_ts(rec: Json) -> float:
    """A stored record's timestamp (our own store: trusted to be a number)."""
    ts = rec.get("ts", 0)
    return cast("float", ts)  # pragma: no mutate -- a runtime no-op


class AttributionError(Exception):
    """A plausible child committer could not be identified safely."""


def without(rec: Json, drop: Mapping[str, object]) -> Json:
    """A record without the keys of ``drop``."""
    return {k: v for k, v in rec.items() if k not in drop}
