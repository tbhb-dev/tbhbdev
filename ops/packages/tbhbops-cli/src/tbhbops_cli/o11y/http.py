# cspell:ignore newurl
"""Bounded loopback HTTP without proxies or redirects."""

import base64
from http.client import HTTPConnection, HTTPException
from typing import IO, Protocol, override
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
lazy from email.message import Message

from tbhbops.o11y.receipt import base_url

MAX_BODY = 4 * 1024 * 1024


class TransportError(Exception):
    """Only the exception class crosses the HTTP boundary."""


class BodyTooLargeError(Exception):
    """The response exceeds the buffering limit."""


class Http(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: int,
    ) -> tuple[int, bytes]: ...


class NoRedirect(HTTPRedirectHandler):
    @override
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


class LocalHttp:
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: int,
    ) -> tuple[int, bytes]:
        base_url(url.partition("?")[0])
        if any(
            key.lower() in {"authorization", "proxy-authorization", "cookie"}
            for key in headers
        ):
            msg = "credentials are forbidden"
            raise ValueError(msg)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        request = Request(url, data=body, headers=headers, method=method)  # noqa: S310  # nosec B310: validated loopback HTTP only
        try:
            try:
                response = opener.open(request, timeout=timeout)  # nosec B310: validated loopback HTTP only
            except HTTPError as exc:
                response = exc
            with response:
                data = response.read(MAX_BODY)
                if response.read(1):
                    raise BodyTooLargeError
                # Bounded reads do not enforce Content-Length at EOF.
                if response.length:
                    msg_0 = "IncompleteRead"
                    raise TransportError(msg_0)
                return response.code, data
        except (OSError, URLError, HTTPException) as exc:
            raise TransportError(type(exc).__name__) from None


class AdminHttp(Protocol):
    def request(
        self, url: str, password: bytes, timeout: int, *, method: str
    ) -> tuple[int, bytes]: ...


class LocalAdminHttp:
    """Recovery login through a bounded direct connection, without redirect handling."""

    def request(
        self, url: str, password: bytes, timeout: int, *, method: str
    ) -> tuple[int, bytes]:
        base_url(url)
        parts = urlsplit(url)
        authorization = base64.b64encode(b"admin:" + password.rstrip(b"\n")).decode()
        connection = HTTPConnection(parts.netloc, timeout=timeout)
        try:
            connection.request(
                method, parts.path, headers={"Authorization": "Basic " + authorization}
            )
            response = connection.getresponse()
            data = response.read(MAX_BODY)
            if response.read(1):
                raise BodyTooLargeError
            if response.length:
                message = "IncompleteRead"
                raise TransportError(message)
        except (OSError, HTTPException) as exc:
            raise TransportError(type(exc).__name__) from None
        finally:
            connection.close()
        return response.status, data
