"""Check Connect request handling against fake endpoints."""

from __future__ import annotations

import functools
import gc
import socket
import traceback
from typing import TYPE_CHECKING, override

import httpx
import pytest
from conftest import SECRETS_FIXTURE_VALUE
from onepasswordconnectsdk.client import Client

from tbhbdev.secrets import connect as transport
from tbhbdev.secrets.connect import SecretsError
from tbhbdev.secrets.store import Store

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import FakeConnect
    from onepasswordconnectsdk.config import ClientConfig

# A tailnet suffix to check for in error messages.
SUFFIX = "example.ts.net"


def closed_url() -> str:
    """A loopback URL nothing listens on, so connecting is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("status", [500, 503])
def test_failover_skips_a_5xx_and_a_refused_endpoint(
    connect: Callable[[], FakeConnect], secrets_server: FakeConnect, status: int
) -> None:
    broken = connect()
    broken.status = status
    backend = transport.Connect(
        secrets_server.token, [broken.url, closed_url(), secrets_server.url]
    )
    assert Store(backend).read("ops/db/password") == SECRETS_FIXTURE_VALUE
    assert broken.requests == [("GET", "/v1/vaults")]
    # The endpoint that answered is tried first from then on.
    assert Store(backend).read("ops/db/user") == "admin"
    assert broken.requests == [("GET", "/v1/vaults")]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_4xx_is_final(
    connect: Callable[[], FakeConnect], secrets_server: FakeConnect, status: int
) -> None:
    refusing = connect()
    refusing.status = status
    backend = transport.Connect(
        secrets_server.token, [refusing.url, secrets_server.url]
    )
    with pytest.raises(SecretsError, match=f"HTTP {status}"):
        backend.vaults()
    assert secrets_server.requests == []


def test_no_endpoint_answering_fails(connect: Callable[[], FakeConnect]) -> None:
    down = connect()
    down.status = 502
    backend = transport.Connect(down.token, [closed_url(), down.url])
    with pytest.raises(SecretsError, match="no Connect endpoint answered"):
        backend.vaults()


# The endpoint that fails_at makes fail before it sends a request.
FIRST = f"https://nas-a.{SUFFIX}"
POSTED = ("POST", "/v1/vaults/v1/items")


def fail_at(monkeypatch: pytest.MonkeyPatch, error: type[Exception]) -> None:
    """Make every request to FIRST raise `error`."""

    class Failing(Client):
        @override
        def build_request(
            self, method: str, path: str, body: object = None
        ) -> httpx.Response:
            if self.url == FIRST:
                message = "fake"
                raise error(message)
            return super().build_request(method, path, body)

    monkeypatch.setattr(transport.connect_client, "Client", Failing)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
def test_a_post_moves_on_when_it_cant_connect(
    monkeypatch: pytest.MonkeyPatch, secrets_server: FakeConnect, error: type[Exception]
) -> None:
    fail_at(monkeypatch, error)
    backend = transport.Connect(secrets_server.token, [FIRST, secrets_server.url])
    assert backend.create("v1", {"title": "x"})["title"] == "x"
    assert secrets_server.requests == [POSTED]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
def test_a_post_an_endpoint_may_have_done_is_final(
    monkeypatch: pytest.MonkeyPatch,
    secrets_server: FakeConnect,
    error: type[Exception],
) -> None:
    fail_at(monkeypatch, error)
    backend = transport.Connect(secrets_server.token, [FIRST, secrets_server.url])
    with pytest.raises(SecretsError) as caught:
        backend.create("v1", {"title": "x"})
    assert str(caught.value) == (
        f"Connect may have done POST /v1/vaults/v1/items: {error.__name__}"
    )
    assert secrets_server.requests == []


def test_a_post_with_a_5xx_is_final(
    connect: Callable[[], FakeConnect], secrets_server: FakeConnect
) -> None:
    broken = connect()
    broken.status = 500
    backend = transport.Connect(secrets_server.token, [broken.url, secrets_server.url])
    with pytest.raises(SecretsError) as caught:
        backend.create("v1", {"title": "x"})
    assert str(caught.value) == "Connect failed POST /v1/vaults/v1/items: HTTP 500"
    assert broken.requests == [POSTED]
    assert secrets_server.requests == []


def first_fails(
    how: str, monkeypatch: pytest.MonkeyPatch, connect: Callable[[], FakeConnect]
) -> str:
    """The URL of an endpoint that times out reading or answers HTTP 503."""
    if how == "timeout":
        fail_at(monkeypatch, httpx.ReadTimeout)
        return FIRST
    broken = connect()
    broken.status = 503
    return broken.url


@pytest.mark.parametrize("how", ["timeout", "5xx"])
def test_a_put_or_delete_moves_on_when_an_endpoint_may_have_done_it(
    monkeypatch: pytest.MonkeyPatch,
    connect: Callable[[], FakeConnect],
    secrets_server: FakeConnect,
    how: str,
) -> None:
    first = first_fails(how, monkeypatch, connect)
    backend = transport.Connect(secrets_server.token, [first, secrets_server.url])
    assert backend.update("v1", "i1", {"title": "x"})["title"] == "x"
    backend.delete("v1", "i1")
    assert secrets_server.requests == [
        ("PUT", "/v1/vaults/v1/items/i1"),
        ("DELETE", "/v1/vaults/v1/items/i1"),
    ]
    assert secrets_server.items["v1"] == {}


@pytest.mark.parametrize("how", ["timeout", "5xx"])
def test_a_delete_that_finds_nothing_after_moving_on_has_succeeded(
    monkeypatch: pytest.MonkeyPatch,
    connect: Callable[[], FakeConnect],
    secrets_server: FakeConnect,
    how: str,
) -> None:
    first = first_fails(how, monkeypatch, connect)
    backend = transport.Connect(secrets_server.token, [first, secrets_server.url])
    backend.delete("v1", "i9")
    assert secrets_server.requests == [("DELETE", "/v1/vaults/v1/items/i9")]


def test_a_delete_that_finds_nothing_after_a_refused_connection_fails(
    secrets_server: FakeConnect,
) -> None:
    backend = transport.Connect(
        secrets_server.token, [closed_url(), secrets_server.url]
    )
    with pytest.raises(SecretsError) as caught:
        backend.delete("v1", "i9")
    assert (
        str(caught.value) == "Connect refused DELETE /v1/vaults/v1/items/i9: HTTP 404"
    )


@pytest.mark.parametrize(
    ("call", "sent", "status"),
    [
        (
            functools.partial(
                transport.Connect.update, vault_id="v1", item_id="i9", item={}
            ),
            "PUT /v1/vaults/v1/items/i9",
            404,
        ),
        (
            functools.partial(transport.Connect.delete, vault_id="v9", item_id="i1"),
            "DELETE /v1/vaults/v9/items/i1",
            403,
        ),
    ],
)
def test_only_a_delete_that_finds_nothing_after_moving_on_succeeds(
    connect: Callable[[], FakeConnect],
    secrets_server: FakeConnect,
    call: Callable[[transport.Connect], object],
    sent: str,
    status: int,
) -> None:
    broken = connect()
    broken.status = 503
    with pytest.raises(SecretsError) as caught:
        call(transport.Connect(secrets_server.token, [broken.url, secrets_server.url]))
    assert str(caught.value) == f"Connect refused {sent}: HTTP {status}"


@pytest.mark.parametrize(
    ("call", "sent"),
    [
        (
            functools.partial(transport.Connect.items, vault_id="v1"),
            "GET /v1/vaults/v1/items",
        ),
        (
            functools.partial(transport.Connect.item, vault_id="v1", item_id="i1"),
            "GET /v1/vaults/v1/items/i1",
        ),
        (
            functools.partial(
                transport.Connect.create, vault_id="v1", item={"title": "x"}
            ),
            "POST /v1/vaults/v1/items",
        ),
        (
            functools.partial(
                transport.Connect.update,
                vault_id="v1",
                item_id="i1",
                item={"title": "x"},
            ),
            "PUT /v1/vaults/v1/items/i1",
        ),
        (
            functools.partial(transport.Connect.delete, vault_id="v1", item_id="i1"),
            "DELETE /v1/vaults/v1/items/i1",
        ),
    ],
)
def test_a_refusal_names_the_request(
    secrets_server: FakeConnect, call: Callable[[transport.Connect], object], sent: str
) -> None:
    secrets_server.status = 404
    with pytest.raises(SecretsError) as caught:
        call(transport.Connect(secrets_server.token, [secrets_server.url]))
    assert str(caught.value) == f"Connect refused {sent}: HTTP 404"


@pytest.mark.parametrize(
    ("raw", "call", "error"),
    [
        (
            b"<html>",
            transport.Connect.vaults,
            "Connect sent an unexpected response to GET /v1/vaults",
        ),
        (b"{}", transport.Connect.vaults, "Connect sent an unexpected response"),
        (
            b"[]",
            functools.partial(transport.Connect.item, vault_id="v1", item_id="i1"),
            "Connect sent an unexpected response",
        ),
    ],
)
def test_an_unexpected_answer_fails(
    secrets_server: FakeConnect,
    raw: bytes,
    call: Callable[[transport.Connect], object],
    error: str,
) -> None:
    secrets_server.raw = raw
    backend = transport.Connect(secrets_server.token, [secrets_server.url])
    with pytest.raises(SecretsError) as caught:
        call(backend)
    assert str(caught.value) == error


def test_each_client_has_the_timeout(
    secrets_server: FakeConnect, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[Client] = []

    class Recording(Client):
        def __init__(
            self, url: str, token: str, config: ClientConfig | None = None
        ) -> None:
            super().__init__(url, token, config)
            made.append(self)

    monkeypatch.setattr(transport.connect_client, "Client", Recording)
    transport.Connect(secrets_server.token, [secrets_server.url]).vaults()
    assert [client.session.timeout for client in made] == [transport.TIMEOUT]


def test_connect_repr_leaves_out_the_token(secrets_server: FakeConnect) -> None:
    assert secrets_server.token not in repr(
        transport.Connect(secrets_server.token, [secrets_server.url])
    )


def assert_url_unseen(
    caught: pytest.ExceptionInfo[SecretsError],
    backend: transport.Connect,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The suffix is in no message, traceback, repr or output."""
    shown = "".join(traceback.format_exception(caught.value))
    captured = capfd.readouterr()
    for text in (str(caught.value), shown, repr(backend), captured.out, captured.err):
        assert SUFFIX not in text
    assert repr(backend) == "Connect(endpoints=1)"


@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize(
    ("url", "kind"),
    [
        # httpx refuses the URL when it builds the client, and names its host.
        (f"https://[nas-a.{SUFFIX}]", "InvalidURL"),
        # idna refuses the host when httpx builds the request.
        (f"https://xn--zz.{SUFFIX}", "IDNAError"),
    ],
)
def test_a_url_that_cant_be_sent_stays_out_of_the_error(
    capfd: pytest.CaptureFixture[str], url: str, kind: str
) -> None:
    backend = transport.Connect("a-token", [url])
    with pytest.raises(SecretsError) as caught:
        backend.vaults()
    assert str(caught.value) == f"Connect can't send GET /v1/vaults: {kind}"
    assert_url_unseen(caught, backend, capfd)
    # A half-made client would fail as it's deleted, here.
    del caught, backend
    gc.collect()


@pytest.mark.parametrize(
    "error",
    [httpx.DecodingError, httpx.TooManyRedirects, httpx.InvalidURL, httpx.StreamError],
)
def test_any_other_httpx_error_is_final_and_hides_the_url(
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    class Failing(Client):
        @override
        def build_request(
            self, method: str, path: str, body: object = None
        ) -> httpx.Response:
            del method, path, body
            message = f"cannot reach {self.url}"
            raise error(message)

    monkeypatch.setattr(transport.connect_client, "Client", Failing)
    backend = transport.Connect("a-token", [f"https://nas-a.{SUFFIX}"])
    with pytest.raises(SecretsError) as caught:
        backend.item("v1", "i1")
    assert str(caught.value) == (
        f"Connect can't send GET /v1/vaults/v1/items/i1: {error.__name__}"
    )
    assert_url_unseen(caught, backend, capfd)
