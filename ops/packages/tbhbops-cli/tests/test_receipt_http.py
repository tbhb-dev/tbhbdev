# cspell:ignore getproxies
"""Real loopback sockets prove the bounded urllib transport."""

import io
import json
import socket
import threading
from dataclasses import replace
from email.message import Message
from http.client import BadStatusLine, IncompleteRead, InvalidURL, LineTooLong
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Never, cast, override
from urllib.request import Request
lazy from pathlib import Path

import pytest
from tbhbops_cli import cli
from tbhbops_cli.o11y.http import (
    MAX_BODY,
    BodyTooLargeError,
    LocalHttp,
    NoRedirect,
    TransportError,
)
from tbhbops_cli.o11y.receipt import receipt

if TYPE_CHECKING:
    from collections.abc import Iterator

URL_TIMEOUT = 15


class Server(ThreadingHTTPServer):
    paths: list[str]
    service: str = ""
    run: str = ""
    stamp: str = ""
    value: str = ""


class Handler(BaseHTTPRequestHandler):
    @property
    def fixture(self) -> Server:
        return cast("Server", self.server)

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass

    def answer(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # http.server callback
        self.fixture.paths.append(self.path)
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/traces":
            self.fixture.service = data["resourceSpans"][0]["resource"]["attributes"][
                0
            ]["value"]["stringValue"]
        elif self.path == "/v1/logs":
            log = data["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
            self.fixture.run = log["body"]["stringValue"].removeprefix("o11y-check ")
            self.fixture.stamp = log["timeUnixNano"]
        elif self.path == "/v1/metrics":
            self.fixture.value = data["resourceMetrics"][0]["scopeMetrics"][0][
                "metrics"
            ][0]["gauge"]["dataPoints"][0]["asInt"]
        self.answer(b"{}")

    def do_GET(self) -> None:  # http.server callback
        if self.path.startswith("/short"):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"short")
            return
        self.fixture.paths.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/followed")
            self.end_headers()
            return
        if self.path in {"/large", "/at-limit"}:
            self.answer(b"x" * (MAX_BODY + (self.path == "/large")))
            return
        if "/tempo/api/traces/" in self.path:
            attribute = {
                "key": "service.name",
                "value": {"stringValue": self.fixture.service},
            }
            data = {"batches": [{"resource": {"attributes": [attribute]}}]}
        elif "/loki/" in self.path:
            data = {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "values": [
                                [self.fixture.stamp, f"o11y-check {self.fixture.run}"]
                            ]
                        }
                    ]
                },
            }
        elif "timestamp(" in self.path:
            data = {
                "data": {
                    "result": [
                        {"value": [0, str(int(self.fixture.stamp) // 1_000_000_000)]}
                    ]
                }
            }
        else:
            data = {"data": {"result": [{"value": [0, self.fixture.value]}]}}
        self.answer(json.dumps(data).encode())


@pytest.fixture(autouse=True)
def no_proxy_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid() -> Never:
        msg = "proxy discovery must not run"
        raise AssertionError(msg)

    # Prevent accidental macOS system-proxy discovery in forked mutation workers.
    monkeypatch.setattr("urllib.request.getproxies", forbid)


@pytest.fixture
def server() -> Iterator[tuple[Server, str]]:
    instance = Server(("127.0.0.1", 0), Handler)
    instance.paths = []
    thread = threading.Thread(target=instance.serve_forever)
    thread.start()
    try:
        yield instance, f"http://127.0.0.1:{instance.server_port}"
    finally:
        instance.shutdown()
        thread.join()
        instance.server_close()


def test_receipt_socket(
    server: tuple[Server, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, url = server

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    state = tmp_path / "state"
    draws: list[int] = []
    ticks: list[float] = [0]

    def random_bytes(size: int) -> bytes:
        assert len(draws) < 20
        draws.append(size)
        return b"\x01" * size

    def monotonic() -> float:
        ticks[0] += 2
        assert ticks[0] < 200
        return ticks[0]

    def sleep(_delay: float) -> None:
        pass

    system = replace(
        cli.real_system(),
        random_bytes=random_bytes,
        monotonic=monotonic,
        sleep=sleep,
        epoch=lambda: 1000.0,
    )
    lines, ok = receipt(system, url, url, state_file=state)
    assert ok
    assert len(lines) == 3
    assert instance.paths[:3] == ["/v1/traces", "/v1/logs", "/v1/metrics"]
    assert len(instance.paths) == 7
    assert all(
        path.startswith("/api/datasources/proxy/uid/") for path in instance.paths[3:]
    )
    assert state.stat().st_mode & 0o777 == 0o600


def test_redirect(server: tuple[Server, str]) -> None:
    instance, url = server
    assert LocalHttp().request("GET", url + "/redirect", None, {}, URL_TIMEOUT) == (
        302,
        b"",
    )
    assert instance.paths == ["/redirect"]


def test_oversized(server: tuple[Server, str]) -> None:
    _, url = server
    with pytest.raises(BodyTooLargeError):
        LocalHttp().request("GET", url + "/large", None, {}, URL_TIMEOUT)


def test_transport() -> None:
    # Bind a port without listening so the request fails deterministically.

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        with pytest.raises(TransportError, match=r"^URLError$"):
            LocalHttp().request(
                "GET", f"http://127.0.0.1:{port}", None, {}, URL_TIMEOUT
            )


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization", "Cookie"])
def test_auth_refusal(header: str) -> None:
    with pytest.raises(ValueError, match=r"^credentials are forbidden$"):
        LocalHttp().request(
            "GET", "http://localhost:1", None, {header: "sensitive"}, URL_TIMEOUT
        )


def test_nonlocal_refusal() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LocalHttp().request("GET", "http://example.test:1", None, {}, URL_TIMEOUT)


def test_redirect_callback() -> None:

    assert (
        NoRedirect().redirect_request(
            Request("http://localhost:1"),
            io.BytesIO(),
            302,
            "Found",
            Message(),
            "http://localhost:1/followed",
        )
        is None
    )


def test_exact_limit(server: tuple[Server, str]) -> None:
    _, url = server
    status, body = LocalHttp().request("GET", url + "/at-limit", None, {}, URL_TIMEOUT)
    assert status == 200
    assert len(body) == MAX_BODY


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("sensitive"),
        IncompleteRead(b"sensitive"),
        BadStatusLine("sensitive"),
        LineTooLong("sensitive"),
        InvalidURL("sensitive"),
    ],
)
def test_transport_class_only(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Never:
        raise error

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fail)
    with pytest.raises(TransportError, match=f"^{type(error).__name__}$"):
        LocalHttp().request("GET", "http://localhost:1", None, {}, URL_TIMEOUT)


def test_request_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[int | None] = []

    class Response(io.BytesIO):
        code = 201
        length: int | None = None

        @override
        def read(self, size: int | None = -1) -> bytes:
            reads.append(size)
            return super().read(size)

    response = Response(b"answer")

    def open_request(_self: object, request: Request, *, timeout: int) -> Response:
        assert request.full_url == "http://localhost:123/path?q=?"
        assert request.get_method() == "PUT"
        assert request.data == b"payload"
        assert dict(request.header_items()) == {"Content-type": "application/json"}
        assert timeout == 15
        return response

    monkeypatch.setattr("urllib.request.OpenerDirector.open", open_request)
    assert LocalHttp().request(
        "PUT",
        "http://localhost:123/path?q=?",
        b"payload",
        {"Content-Type": "application/json"},
        15,
    ) == (201, b"answer")
    assert reads == [MAX_BODY, 1]
    assert response.closed


def test_short_body(server: tuple[Server, str], tmp_path: Path) -> None:
    _, url = server
    state = tmp_path / "state"
    state.write_text("0123456789abcdef " + "01" * 16 + " 12 1000\n")
    ticks = iter([0, 0, 60, 60])
    system = replace(cli.real_system(), monotonic=lambda: next(ticks))
    out, err = io.StringIO(), io.StringIO()
    assert (
        cli.run(
            [
                "o11y",
                "receipt",
                "--grafana-url",
                url + "/short",
                "--signals",
                "logs",
                "--recheck",
                str(state),
            ],
            system,
            out,
            err,
        )
        == 1
    )
    assert out.getvalue().splitlines() == [
        (
            "FAIL receipt Loki: case=transport-error transport=IncompleteRead "
            "query_http=unavailable floor_s=0 elapsed_s=60 no POST"
        ),
        "FAIL receipt: the log line of run 0123456789abcdef is no longer returned",
    ]
    assert err.getvalue() == ""
