"""The recovery transport keeps Basic credentials in memory on loopback."""

import base64
import io
from http.client import IncompleteRead
from unittest.mock import Mock

import pytest
from tbhbops_cli.o11y.http import (
    MAX_BODY,
    BodyTooLargeError,
    LocalAdminHttp,
    LocalHttp,
    TransportError,
)


def connection(monkeypatch: pytest.MonkeyPatch, response: io.BytesIO) -> Mock:
    instance = Mock()
    instance.getresponse.return_value = response
    constructor = Mock(return_value=instance)
    monkeypatch.setattr("tbhbops_cli.o11y.http.HTTPConnection", constructor)
    return constructor


class Response(io.BytesIO):
    status = 201
    length: int | None = None


def test_admin_request_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Response(b"answer")
    read = Mock(wraps=response.read)
    monkeypatch.setattr(response, "read", read)
    constructor = connection(monkeypatch, response)
    assert LocalAdminHttp().request(
        "http://127.0.0.1:123/api/admin/settings", b"synthetic\n", 15, method="GET"
    ) == (201, b"answer")
    constructor.assert_called_once_with("127.0.0.1:123", timeout=15)
    instance = constructor.return_value
    instance.request.assert_called_once_with(
        "GET",
        "/api/admin/settings",
        headers={
            "Authorization": "Basic " + base64.b64encode(b"admin:synthetic").decode()
        },
    )
    assert [call.args for call in read.call_args_list] == [(MAX_BODY,), (1,)]
    instance.close.assert_called_once_with()


@pytest.mark.parametrize(("size", "fails"), [(MAX_BODY, False), (MAX_BODY + 1, True)])
def test_admin_body_cap(
    monkeypatch: pytest.MonkeyPatch, size: int, fails: bool
) -> None:
    constructor = connection(monkeypatch, Response(b"x" * size))
    if fails:
        with pytest.raises(BodyTooLargeError):
            LocalAdminHttp().request(
                "http://localhost:123/admin", b"synthetic", 15, method="GET"
            )
    else:
        assert LocalAdminHttp().request(
            "http://localhost:123/admin", b"synthetic", 15, method="GET"
        ) == (201, b"x" * MAX_BODY)
    constructor.return_value.close.assert_called_once_with()


@pytest.mark.parametrize(
    "error", [OSError("private-password"), IncompleteRead(b"private-password")]
)
def test_admin_transport_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    constructor = connection(monkeypatch, Response())
    constructor.return_value.request = Mock(side_effect=error)
    with pytest.raises(TransportError, match=r"^" + type(error).__name__ + "$"):
        LocalAdminHttp().request(
            "http://localhost:123/admin", b"synthetic", 15, method="GET"
        )
    constructor.return_value.close.assert_called_once_with()


def test_admin_short_response_and_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Response(b"short")
    response.length = 100
    constructor = connection(monkeypatch, response)
    with pytest.raises(TransportError, match=r"^IncompleteRead$"):
        LocalAdminHttp().request(
            "http://localhost:123/admin", b"synthetic", 15, method="GET"
        )
    constructor.return_value.close.assert_called_once_with()
    response = Response()
    response.status = 302
    constructor = connection(monkeypatch, response)
    assert LocalAdminHttp().request(
        "http://localhost:123/admin", b"synthetic", 15, method="GET"
    ) == (
        302,
        b"",
    )
    constructor.return_value.request.assert_called_once()
    constructor.return_value.close.assert_called_once_with()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid:123/admin",
        "http://user:password@localhost:123/admin",
        "https://localhost:123/admin",
    ],
)
def test_admin_loopback_only(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = connection(monkeypatch, Response())
    with pytest.raises(ValueError, match="loopback"):
        LocalAdminHttp().request(url, b"synthetic", 15, method="GET")
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "header", ["Authorization", "authorization", "Proxy-Authorization", "Cookie"]
)
def test_receipt_http_still_refuses_auth(header: str) -> None:
    with pytest.raises(ValueError, match="credentials are forbidden"):
        LocalHttp().request(
            "GET", "http://localhost:123", None, {header: "private"}, 15
        )
    with pytest.raises(ValueError, match="loopback"):
        LocalHttp().request("GET", "http://admin:private@localhost:123", None, {}, 15)


@pytest.mark.parametrize("password", [b"syntheticX\n", b"synthetic \n"])
def test_admin_preserves_password_suffix(
    monkeypatch: pytest.MonkeyPatch, password: bytes
) -> None:
    constructor = connection(monkeypatch, Response())
    LocalAdminHttp().request("http://localhost:123/admin", password, 15, method="GET")
    header = constructor.return_value.request.call_args.kwargs["headers"][
        "Authorization"
    ]
    assert base64.b64decode(header.removeprefix("Basic ")) == b"admin:" + password[:-1]
