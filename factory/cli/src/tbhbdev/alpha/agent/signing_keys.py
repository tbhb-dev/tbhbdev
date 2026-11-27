"""Read the agent account's public SSH signing keys from GitHub."""

from __future__ import annotations

import json
from typing import cast

import httpx2

from tbhbdev import github_tls

KEYS_URL = "https://api.github.com/users/tbhb-agent/ssh_signing_keys?per_page=100"
MAX_KEYS_BYTES = 1024 * 1024


def public_keys(
    *, timeout: int, transport: httpx2.BaseTransport | None = None
) -> list[str]:
    """Read tbhb-agent's public signing keys in one GitHub request.

    Redirects are not followed: anything but a 200 is refused, as is a body over
    `MAX_KEYS_BYTES`.
    """
    body = bytearray()
    try:
        with (
            httpx2.Client(
                timeout=timeout,
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                verify=github_tls.public_roots(),
                headers={"Accept": "application/vnd.github+json"},
            ) as client,
            client.stream("GET", KEYS_URL) as response,
        ):
            if response.status_code != httpx2.codes.OK:
                message = f"GitHub answered {response.status_code}"
                raise OSError(message)
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_KEYS_BYTES:
                    message = "the GitHub signing key list is too large"
                    raise OSError(message)
    except httpx2.HTTPError as exc:
        message = f"the GitHub request failed: {type(exc).__name__}"
        raise OSError(message) from None
    entries: object = json.loads(body)
    if not isinstance(entries, list):
        message = "expected a GitHub signing key list"
        raise TypeError(message)
    keys: list[str] = []
    for entry in cast("list[dict[str, object]]", entries):
        key = entry["key"]
        if not isinstance(key, str):
            message = "expected an OpenSSH public key string"
            raise TypeError(message)
        keys.append(key)
    return keys
