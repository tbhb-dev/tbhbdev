"""Tests for the HTTPS call and the error type."""

import io
import urllib.error
import urllib.request
from email.message import Message
from typing import Self

import pytest
from tbhbops_cli import common


class Response:
    """What urlopen returns: a context manager with a status and a body."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_a_request_goes_out_over_https_and_the_answer_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[urllib.request.Request, object]] = []

    def urlopen(request: urllib.request.Request, timeout: int) -> Response:
        sent.append((request, timeout))
        return Response(201, b"answer")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    answer = common.https_request("PUT", "https://a.example/x", {"X-Y": "z"}, b"body")
    assert answer == (201, b"answer")
    ((request, timeout),) = sent
    assert request.get_method() == "PUT"
    assert request.full_url == "https://a.example/x"
    assert request.get_header("X-y") == "z"
    assert request.data == b"body"
    assert timeout == common.TIMEOUT


@pytest.mark.parametrize("url", ["http://a.example/x", "ftp://a.example", "file:///x"])
def test_anything_but_https_is_refused(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def urlopen(*_args: object, **_kwargs: object) -> Response:
        pytest.fail("a request went out")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(common.CliError, match="anything but https") as raised:
        common.https_request("GET", url, {}, None)
    assert url not in str(raised.value)
    assert str(raised.value) == "refusing to send a request over anything but https"


def test_an_http_error_answers_with_its_status_and_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(request: urllib.request.Request, **_kwargs: object) -> Response:
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "no",
            Message(),
            io.BytesIO(b"secret"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert common.https_request("GET", "https://a.example", {}, None) == (403, b"")


def test_a_connection_failure_names_its_type_and_never_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(*_args: object, **_kwargs: object) -> Response:
        msg = "host.secret-domain.example unreachable"
        raise urllib.error.URLError(msg)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(common.CliError) as raised:
        common.https_request("GET", "https://host.secret-domain.example", {}, None)
    assert str(raised.value) == "a request failed: URLError"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
