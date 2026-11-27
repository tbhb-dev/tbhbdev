"""Send Connect requests through the SDK with endpoint failover."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import httpx2
from onepasswordconnectsdk import client as connect_client
from onepasswordconnectsdk.config import ClientConfig
from onepasswordconnectsdk.utils import PathBuilder

if TYPE_CHECKING:
    from collections.abc import Sequence

type Json = dict[str, object]

# The Connect SDK creates httpx sessions and raises httpx exceptions.
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
CLIENT_ERROR = 400
NOT_FOUND = 404
SERVER_ERROR = 500
# The methods a second endpoint may repeat after the first may have received
# them. Any other, such as POST, could make a second item.
IDEMPOTENT = frozenset({"GET", "PUT", "DELETE"})


class SecretsError(Exception):
    """A secrets operation failed. The message never holds a value or token."""


def _object(value: object) -> Json:
    if not isinstance(value, dict):
        message = "Connect sent an unexpected response"
        raise SecretsError(message)
    return value


def objects(value: object) -> list[Json]:
    if not isinstance(value, list):
        message = "Connect sent an unexpected response"
        raise SecretsError(message)
    entries: list[object] = value
    return [_object(entry) for entry in entries]


class Connect:
    """The Connect API at each endpoint in turn, through 1Password's SDK.

    A failure to connect moves on to the next endpoint. So does any other
    connection error or a 5xx answer to a GET, PUT or DELETE, but not to a
    POST, which the endpoint may have done already. A 4xx answer is final,
    except that a DELETE that finds nothing once an endpoint may have done it
    has succeeded. The endpoint that last answered is tried first. Neither
    its repr nor its errors show a URL, which holds the tailnet's suffix.
    """

    def __init__(self, token: str, urls: Sequence[str]) -> None:
        self._token = token
        self._urls = list(urls)
        self._clients: dict[str, connect_client.Client] = {}

    def __repr__(self) -> str:
        return f"Connect(endpoints={len(self._urls)})"

    def _client(self, url: str) -> connect_client.Client:
        if url not in self._clients:
            # An SDK client whose constructor fails raises again as it's deleted,
            # so a URL httpx refuses never reaches one.
            httpx.URL(url)
            self._clients[url] = self._new_client(url)
        return self._clients[url]

    def _new_client(self, url: str) -> connect_client.Client:
        config = ClientConfig(timeout=TIMEOUT, verify=httpx2.create_ssl_context())
        return connect_client.Client(url, self._token, config)

    def _send(
        self, url: str, method: str, path: str, body: Json | None
    ) -> tuple[httpx.Response | None, bool]:
        """The endpoint's answer, or None to move on to the next one.

        The flag says whether the endpoint may have done the request already.
        """
        try:
            response = self._client(url).build_request(method, path, body)
        except httpx.ConnectError, httpx.ConnectTimeout:
            return None, False
        except httpx.TransportError as exc:
            if method in IDEMPOTENT:
                return None, True
            kind = type(exc).__name__
            message = f"Connect may have done {method} {path}: {kind}"
            raise SecretsError(message) from None
        # Any other failure to build or send the request is final. Its
        # message can hold the URL, so neither it nor its traceback shows.
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.StreamError,
            UnicodeError,
        ) as exc:
            message = f"Connect can't send {method} {path}: {type(exc).__name__}"
            raise SecretsError(message) from None
        status = response.status_code
        if status < SERVER_ERROR:
            return response, False
        if method in IDEMPOTENT:
            return None, True
        message = f"Connect failed {method} {path}: HTTP {status}"
        raise SecretsError(message)

    def _call(self, method: str, path: str, body: Json | None = None) -> object:
        # Whether an endpoint passed over may have done the request already.
        # None is false as well, so the pragma hides an equivalent mutant.
        repeated = False  # pragma: no mutate
        for url in list(self._urls):
            response, done = self._send(url, method, path, body)
            repeated = repeated or done
            if response is None:
                continue
            status = response.status_code
            # A DELETE an endpoint passed over may have done finds nothing.
            if repeated and method == "DELETE" and status == NOT_FOUND:
                return None
            if status >= CLIENT_ERROR:
                message = f"Connect refused {method} {path}: HTTP {status}"
                raise SecretsError(message)
            self._urls.remove(url)
            self._urls.insert(0, url)
            if not response.content:
                return None
            try:
                data: object = response.json()
            except ValueError as exc:
                message = f"Connect sent an unexpected response to {method} {path}"
                raise SecretsError(message) from exc
            return data
        message = f"no Connect endpoint answered {method} {path}"
        raise SecretsError(message)

    def vaults(self) -> list[Json]:
        return objects(self._call("GET", PathBuilder().vaults().build()))

    def items(self, vault_id: str) -> list[Json]:
        path = PathBuilder().vaults(vault_id).items().build()
        return objects(self._call("GET", path))

    def item(self, vault_id: str, item_id: str) -> Json:
        path = PathBuilder().vaults(vault_id).items(item_id).build()
        return _object(self._call("GET", path))

    def create(self, vault_id: str, item: Json) -> Json:
        path = PathBuilder().vaults(vault_id).items().build()
        return _object(self._call("POST", path, item))

    def update(self, vault_id: str, item_id: str, item: Json) -> Json:
        path = PathBuilder().vaults(vault_id).items(item_id).build()
        return _object(self._call("PUT", path, item))

    def delete(self, vault_id: str, item_id: str) -> None:
        path = PathBuilder().vaults(vault_id).items(item_id).build()
        self._call("DELETE", path)
