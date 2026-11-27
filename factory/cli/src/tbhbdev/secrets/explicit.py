"""Read an item from explicitly supplied Connect endpoints and credentials."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx
import httpx2
from onepasswordconnectsdk import client as connect_client

from tbhbdev.secrets.connect import NOT_FOUND, TIMEOUT, Connect, SecretsError
from tbhbdev.secrets.store import Store

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tbhbdev.secrets.connect import Json
    from tbhbdev.secrets.store import Item


class ConnectUnavailableError(SecretsError):
    """Every explicitly supplied endpoint failed."""


class ExplicitItemMissingError(SecretsError):
    """Connect could not find the requested item."""


class _ExplicitClient(connect_client.Client):
    @override  # noqa: V105  # The SDK calls this session factory.
    def create_session(self, url: str, token: str) -> httpx.Client:
        # The SDK's default session reads a timeout from the environment even
        # with explicit config. Keep its protocol and headers, but no defaults.
        return httpx.Client(
            base_url=url,
            headers=self.build_headers(token),
            timeout=TIMEOUT,
            trust_env=False,
            verify=httpx2.create_ssl_context(trust_env=False),
        )


class _ExplicitConnect(Connect):
    @override
    def _new_client(self, url: str) -> connect_client.Client:
        return _ExplicitClient(url, self._token)

    @override
    def _send(
        self, url: str, method: str, path: str, body: Json | None
    ) -> tuple[httpx.Response | None, bool]:
        response, done = super()._send(url, method, path, body)
        if response is not None and response.status_code == NOT_FOUND:
            raise ExplicitItemMissingError
        return response, done

    @override
    def _call(self, method: str, path: str, body: Json | None = None) -> object:
        try:
            return super()._call(method, path, body)
        except ExplicitItemMissingError:
            raise
        except SecretsError:
            raise ConnectUnavailableError from None


def explicit_item(token: str, vault: str, item: str, *, urls: Sequence[str]) -> Item:
    """Read with explicit credentials, without consulting user configuration.

    Missing names raise SecretsError; transport and protocol failures try every
    endpoint before raising ConnectUnavailableError.
    """
    for url in urls:
        try:
            return Store(_ExplicitConnect(token, [url])).get(vault, item)
        except ConnectUnavailableError:
            continue
    raise ConnectUnavailableError
