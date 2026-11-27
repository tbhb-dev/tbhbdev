"""Check error mapping for an explicit item lookup."""

import httpx
import pytest

from tbhbdev.secrets import explicit
from tbhbdev.secrets.connect import Connect


@pytest.mark.parametrize("status", [404, 403])
def test_explicit_protocol_errors(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    def send(*_args: object) -> tuple[httpx.Response, bool]:
        return httpx.Response(status), False

    monkeypatch.setattr(Connect, "_send", send)
    error = (
        explicit.ExplicitItemMissingError
        if status == 404
        else explicit.ConnectUnavailableError
    )
    with pytest.raises(error):
        explicit.explicit_item(
            "fixture", "vault", "app", urls=["https://example.invalid"]
        )
