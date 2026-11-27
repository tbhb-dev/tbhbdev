# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Merge attribution trailers and validate their timestamps."""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

KEY_GEN = "Generated-by"
KEY_ASSIST = "Assisted-by"
START_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
START_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)
START_ATTR = re.compile(r"(?<![^\s;])started_at=([^\s;]+)")


def utc(timestamp: float) -> str:
    """A UTC ISO 8601 time with whole seconds and a Z suffix."""
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_ok(value: object) -> str | None:
    """A ``started_at`` value (case preserved) if it is a real UTC time."""
    if not isinstance(value, str) or not START_RE.fullmatch(value):
        return None
    try:
        datetime.strptime(value, START_FORMAT).replace(tzinfo=UTC)  # DTZ007
    except ValueError:
        return None
    return value


def trailer_start(value: str) -> str:
    """A trailer value's valid ``started_at``, or an empty string without one."""
    found = START_ATTR.search(value)
    return (found and start_ok(found[1])) or ""


def replace_trailer_fields(value: str, fields: Mapping[str, str | None]) -> str:
    """Update only the specified attributes in the trailer's attribute blob."""
    blob = next(token for token in value.split() if "=" in token)
    pairs = [pair for pair in blob.split(";") if pair.partition("=")[0] not in fields]
    pairs.extend(f"{key}={field}" for key, field in fields.items() if field)
    return value.replace(blob, ";".join(sorted(pairs)))


def identity_key(value: str) -> tuple[str, ...]:
    """Who an LLM trailer value names: harness slug, model, session, subagent."""
    blob = value.split()[1]
    attrs = {
        key: val for key, _, val in (pair.partition("=") for pair in blob.split(";"))
    }
    return (
        attrs["harness"].partition(":")[0],
        attrs.get("model", ""),
        attrs.get("session", ""),
        attrs.get("subagent", ""),
    )


def merge_pr_trailers(
    found: Sequence[tuple[str, str]], posting: str | None
) -> list[tuple[str, str]]:
    """Deduplicate (key, value) trailers by identity; Generated-by wins.

    The posting agent's value is added as Assisted-by when its identity is
    absent. Generated-by lines come first, as the commit-msg check requires.
    Each identity keeps its oldest commit trailer timestamp.
    """
    roles: dict[tuple[str, ...], tuple[str, str]] = {}
    starts: dict[tuple[str, ...], str] = {}
    for key, value in found:
        ident = identity_key(value)
        if start := trailer_start(value):
            starts[ident] = min(start, starts.get(ident, start))
        if ident not in roles or (key == KEY_GEN and roles[ident][0] != key):
            roles[ident] = (key, value)
    if posting is not None and identity_key(posting) not in roles:
        roles[identity_key(posting)] = (KEY_ASSIST, posting)
    for ident, start in starts.items():
        key, value = roles[ident]
        if trailer_start(value) != start:
            value = replace_trailer_fields(value, {"started_at": start})
            roles[ident] = key, value
    return sorted(roles.values(), key=lambda pair: pair[0] != KEY_GEN)
