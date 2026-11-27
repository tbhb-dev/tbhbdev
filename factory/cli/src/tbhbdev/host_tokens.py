# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Names, lifetimes, and rotation decisions for agent host Connect tokens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

LIFETIME = timedelta(days=90)
_RENEW_WITHIN = timedelta(days=14)
_STAMP = "%Y%m%dT%H%M%SZ"
_NAME_SEPARATORS = 3


@dataclass(frozen=True)
class Token:
    """A Connect token, as its name describes it."""

    id: str
    name: str
    host: str
    created: datetime
    vaults: frozenset[str]

    @property
    def expires(self) -> datetime:
        return self.created + LIFETIME

    def needs_renewal(self, when: datetime) -> bool:
        return self.expires - when <= _RENEW_WITHIN


def token_name(
    host: str, created: datetime, vaults: Iterable[str], *, prefix: str
) -> str:
    stamp = created.strftime(_STAMP)
    return f"{prefix}:{host}:{stamp}:{','.join(sorted(vaults))}"


def has_token_name_shape(name: str) -> bool:
    """Whether NAME has separators for a prefix, host, timestamp and vaults."""
    return name.count(":") >= _NAME_SEPARATORS


def parse_token(name: str, token_id: str, *, prefix: str) -> Token | None:
    """The agent host token NAME describes, or None for any other token."""
    if not has_token_name_shape(name):
        return None
    found_prefix, host, stamp, vaults = name.split(":", _NAME_SEPARATORS)
    if found_prefix != prefix:
        return None
    try:
        created = datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return Token(token_id, name, host, created, frozenset(vaults.split(",")))


def rotation_reason(
    token: Token | None, vaults: frozenset[str], when: datetime, *, held: str | None
) -> str | None:
    """Why the host needs a new token, or None when its token is current.

    HELD is the name the keychain records, or None when it has no token.
    """
    if token is None:
        return "no token"
    if token.vaults != vaults:
        return f"the token {token.name} has other vaults"
    if token.needs_renewal(when):
        return f"the token {token.name} expires {token.expires:%Y-%m-%d}"
    if held is None:
        return "the keychain has no token"
    if held != token.name:
        return f"the keychain holds {held or 'an unrecorded token'}, not {token.name}"
    return None
