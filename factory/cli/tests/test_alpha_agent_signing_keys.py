"""The signing key parity check compares fingerprints, never key text."""

from __future__ import annotations

import shutil

import httpx2
import pytest

from tbhbdev.alpha.agent import signing_keys

pytestmark = pytest.mark.skipif(  # noqa: V107
    shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"
)


def keys_transport(
    status: int, body: bytes, headers: dict[str, str] | None = None
) -> httpx2.MockTransport:
    return httpx2.MockTransport(
        lambda _request: httpx2.Response(status, content=body, headers=headers)
    )


@pytest.mark.parametrize("status", [301, 302, 307, 404, 500])
def test_public_keys_refuses_any_non_200(status: int) -> None:
    transport = keys_transport(status, b"[]", {"Location": "http://evil.example/"})
    with pytest.raises(OSError, match=str(status)):
        signing_keys.public_keys(timeout=5, transport=transport)


def test_public_keys_refuses_an_oversized_body() -> None:
    body = b"[" + b" " * signing_keys.MAX_KEYS_BYTES + b"]"
    with pytest.raises(OSError, match="too large"):
        signing_keys.public_keys(timeout=5, transport=keys_transport(200, body))


def test_public_keys_reports_a_failed_request() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        message = "offline"
        raise httpx2.ConnectError(message, request=request)

    with pytest.raises(OSError, match="ConnectError"):
        signing_keys.public_keys(timeout=5, transport=httpx2.MockTransport(refuse))
