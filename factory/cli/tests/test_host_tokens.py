# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Test Connect token decisions using supplied names, vaults, and times."""

from datetime import UTC, datetime, timedelta

import pytest

from tbhbdev import host_tokens

PREFIX = "example-host"
CREATED = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
VAULTS = frozenset({"vault-all", "vault-h", "a:b"})
NAME = "example-host:h:20261104T120000Z:a:b,vault-all,vault-h"
TOKEN = host_tokens.Token("t", NAME, "h", CREATED, VAULTS)


@pytest.mark.parametrize(
    "name", ["ci", "example-host:h:bad:v", "other:h:20261104T120000Z:v"]
)
def test_parse_token_ignores_other_names(name: str) -> None:
    assert host_tokens.parse_token(name, "t", prefix=PREFIX) is None


def test_token_name_round_trips_vaults_with_colons() -> None:
    name = host_tokens.token_name(
        "h", CREATED, ("vault-h", "vault-all", "a:b"), prefix=PREFIX
    )
    assert name == NAME
    assert host_tokens.parse_token(name, "t", prefix=PREFIX) == TOKEN


@pytest.mark.parametrize(
    ("token", "vaults", "held", "days", "reason"),
    [
        (None, VAULTS, NAME, 0, "no token"),
        (TOKEN, VAULTS | {"ops"}, NAME, 0, f"the token {NAME} has other vaults"),
        (TOKEN, VAULTS, NAME, 75, None),
        (TOKEN, VAULTS, NAME, 76, f"the token {NAME} expires 2027-02-02"),
        (TOKEN, VAULTS, None, 0, "the keychain has no token"),
        (TOKEN, VAULTS, "", 0, f"the keychain holds an unrecorded token, not {NAME}"),
        (TOKEN, VAULTS, "old", 0, f"the keychain holds old, not {NAME}"),
    ],
)
def test_rotation_reason(
    token: host_tokens.Token | None,
    vaults: frozenset[str],
    held: str | None,
    days: int,
    reason: str | None,
) -> None:
    when = CREATED + timedelta(days=days)
    assert host_tokens.rotation_reason(token, vaults, when, held=held) == reason
