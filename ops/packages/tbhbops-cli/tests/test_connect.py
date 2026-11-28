"""Tests for the Connect client."""

import json
lazy from collections.abc import Mapping

import pytest
from tbhbops_cli import connect
from tbhbops_cli.common import CliError

HOST = "https://connect.example/"
VAULT = "vault-id"
FIELD: dict[str, object] = {
    "id": "config",
    "label": "config",
    "type": "CONCEALED",
    "value": "NEW",
}


class Server:
    """A fake Connect server holding one vault's items."""

    def __init__(self, items: list[dict[str, object]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str, str, Mapping[str, str], object]] = []
        self.listed: object = None
        self.status = 200
        self.raw: bytes | None = None

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append(
            (method, url, headers, None if body is None else json.loads(body))
        )
        if self.raw is not None or self.status != 200:
            return self.status, self.raw or b"secret body"
        path = url.removeprefix("https://connect.example")
        if path.startswith(f"/v1/vaults/{VAULT}/items?"):
            summaries = [{"id": i["id"], "title": i["title"]} for i in self.items]
            return 200, json.dumps(
                self.listed if self.listed is not None else summaries
            ).encode()
        if method == "GET" and path.startswith(f"/v1/vaults/{VAULT}/items/"):
            wanted = path.rsplit("/", 1)[1]
            return 200, json.dumps(
                next(i for i in self.items if i["id"] == wanted)
            ).encode()
        return 200, b"{}"


def client(server: Server) -> connect.Connect:
    return connect.Connect(server, HOST, "the-token")


def item(item_id: str = "item-1", *fields: dict[str, object]) -> dict[str, object]:
    return {
        "id": item_id,
        "title": "scanner-policy",
        "vault": {"id": VAULT},
        "fields": list(fields),
    }


def test_a_call_sends_the_token_as_a_bearer_and_joins_the_path_to_the_host() -> None:
    server = Server()
    answer = client(server).call("PUT", "/v1/x", "doing x", {"a": 1})
    assert answer == {}
    assert server.calls == [
        (
            "PUT",
            "https://connect.example/v1/x",
            {"Authorization": "Bearer the-token", "Content-Type": "application/json"},
            {"a": 1},
        )
    ]


def test_a_call_without_a_body_sends_none() -> None:
    server = Server()
    client(server).call("GET", "/v1/x", "doing x")
    assert server.calls[0][3] is None


def test_a_call_that_is_not_a_2xx_is_reported_by_status_alone() -> None:
    server = Server()
    server.status = 403
    with pytest.raises(CliError) as raised:
        client(server).call("GET", "/v1/x", "reading x")
    assert str(raised.value) == "reading x failed: HTTP 403"


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_every_2xx_is_a_success(status: int) -> None:
    server = Server()
    server.status = status
    server.raw = b"[]"
    assert client(server).call("GET", "/v1/x", "reading x") == []


@pytest.mark.parametrize("status", [199, 300, 404, 500])
def test_nothing_outside_the_2xx_is_a_success(status: int) -> None:
    server = Server()
    server.status = status
    server.raw = b"[]"
    with pytest.raises(CliError, match=f"HTTP {status}"):
        client(server).call("GET", "/v1/x", "reading x")


def test_an_answer_that_is_not_json_is_refused_without_quoting_it() -> None:
    server = Server()
    server.raw = b"secret"
    with pytest.raises(CliError) as raised:
        client(server).call("GET", "/v1/x", "reading x")
    assert str(raised.value) == "reading x printed something other than JSON"
    assert raised.value.__suppress_context__


def test_the_vaults_are_listed_by_name() -> None:
    server = Server()
    server.raw = json.dumps(
        [
            {"id": "i1", "name": "all"},
            {"id": "i2", "name": "x"},
            {"id": 3, "name": "n"},
            {"name": "m"},
            {"id": "i4", "name": 4},
            "s",
        ]
    ).encode()
    assert connect.vaults(client(server)) == {"all": "i1", "x": "i2"}
    assert server.calls[0][:2] == ("GET", "https://connect.example/v1/vaults")


@pytest.mark.parametrize("answer", [b"[]", b"{}", b'"x"', b'[{"id": 1}]'])
def test_a_token_that_reaches_no_vault_is_an_error(answer: bytes) -> None:
    server = Server()
    server.raw = answer
    with pytest.raises(CliError) as raised:
        connect.vaults(client(server))
    assert str(raised.value) == "the Connect token reaches no vaults"


def test_a_missing_item_is_created_in_the_vault() -> None:
    server = Server()
    assert connect.publish(client(server), VAULT, "NEW", write=True) == "created"
    list_call, create_call = server.calls
    assert list_call[:2] == (
        "GET",
        "https://connect.example/v1/vaults/vault-id/items?filter=title%20eq%20%22scanner-policy%22",
    )
    assert create_call[0:2] == (
        "POST",
        "https://connect.example/v1/vaults/vault-id/items",
    )
    assert create_call[3] == {
        "vault": {"id": VAULT},
        "title": "scanner-policy",
        "category": "SECURE_NOTE",
        "fields": [FIELD],
    }


def test_a_dry_run_reads_and_writes_nothing() -> None:
    server = Server()
    assert connect.publish(client(server), VAULT, "NEW", write=False) == "created"
    assert [c[0] for c in server.calls] == ["GET"]
    server = Server([item("item-1", {**FIELD, "value": "OLD"})])
    assert connect.publish(client(server), VAULT, "NEW", write=False) == "updated"
    assert [c[0] for c in server.calls] == ["GET", "GET"]


def test_a_current_item_is_left_alone() -> None:
    server = Server([item("item-1", {**FIELD, "value": "NEW"})])
    assert connect.publish(client(server), VAULT, "NEW", write=True) == "current"
    assert [c[0] for c in server.calls] == ["GET", "GET"]


def test_an_item_with_another_value_is_updated_and_its_other_fields_are_kept() -> None:
    other: dict[str, object] = {"id": "o", "label": "other", "value": "keep"}
    old: dict[str, object] = {**FIELD, "value": "OLD"}
    server = Server([item("item-1", other, old)])
    assert connect.publish(client(server), VAULT, "NEW", write=True) == "updated"
    method, url, _headers, body = server.calls[-1]
    assert (method, url) == (
        "PUT",
        "https://connect.example/v1/vaults/vault-id/items/item-1",
    )
    assert isinstance(body, dict)
    assert body["fields"] == [other, FIELD]
    assert body["id"] == "item-1"
    assert body["vault"] == {"id": VAULT}


def test_an_item_without_the_field_gets_it() -> None:
    server = Server([item("item-1")])
    assert connect.publish(client(server), VAULT, "NEW", write=True) == "updated"
    assert server.calls[-1][3]["fields"] == [FIELD]  # pyrefly: ignore


def test_two_values_in_the_field_are_not_current() -> None:
    twice: list[dict[str, object]] = [
        {**FIELD, "value": "NEW"},
        {**FIELD, "value": "NEW"},
    ]
    server = Server([item("item-1", *twice)])
    assert connect.publish(client(server), VAULT, "NEW", write=True) == "updated"


def test_an_item_that_is_not_an_object_is_replaced_by_one_with_the_field() -> None:
    server = Server([item("item-1")])
    original = server.__call__

    def http(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        if method == "GET" and url.endswith("/items/item-1"):
            return 200, b'[["id", "zzz"]]'
        return original(method, url, headers, body)

    assert (
        connect.publish(connect.Connect(http, HOST, "t"), VAULT, "NEW", write=True)
        == "updated"
    )
    assert server.calls[-1][3] == {"fields": [FIELD]}


def test_other_titles_the_server_returns_are_ignored() -> None:
    server = Server()
    server.listed = [{"id": "x", "title": "other"}]
    assert connect.publish(client(server), VAULT, "NEW", write=True) == "created"


def test_two_items_with_the_title_are_an_error() -> None:
    server = Server([item("a"), item("b")])
    with pytest.raises(CliError) as raised:
        connect.publish(client(server), VAULT, "NEW", write=True)
    assert str(raised.value) == "host-0-shared holds 2 items named scanner-policy"


@pytest.mark.parametrize("item_id", [None, "", 3])
def test_an_item_without_an_id_is_an_error(item_id: object) -> None:
    server = Server()
    server.listed = [{"id": item_id, "title": "scanner-policy"}]
    with pytest.raises(CliError) as raised:
        connect.publish(client(server), VAULT, "NEW", write=True)
    assert str(raised.value) == "listing items printed scanner-policy without an id"


def test_the_item_id_is_quoted_in_the_path() -> None:
    urls: list[str] = []

    def http(
        _method: str, url: str, _headers: Mapping[str, str], _body: bytes | None
    ) -> tuple[int, bytes]:
        urls.append(url)
        if "filter" in url:
            return 200, b'[{"id": "a/b c", "title": "scanner-policy"}]'
        return 200, b"{}"

    assert (
        connect.publish(connect.Connect(http, HOST, "t"), VAULT, "NEW", write=False)
        == "updated"
    )
    assert urls[-1] == "https://connect.example/v1/vaults/vault-id/items/a%2Fb%20c"


def test_the_vault_id_is_quoted_in_the_path() -> None:
    server = Server()
    assert connect.publish(client(server), "v/1", "NEW", write=False) == "created"
    assert "/v1/vaults/v%2F1/items?" in server.calls[0][1]


def test_a_failing_call_names_what_failed_by_ref_and_never_the_text() -> None:
    server = Server([item("item-1")])
    original = server.__call__

    def http(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        if method == "PUT":
            return 500, b"echo of NEW"
        return original(method, url, headers, body)

    with pytest.raises(CliError) as raised:
        connect.publish(connect.Connect(http, HOST, "t"), VAULT, "NEW", write=True)
    assert (
        str(raised.value)
        == "updating op:\x2f/host-0-shared/scanner-policy/config failed: HTTP 500"
    )


def test_a_host_is_joined_to_a_path_after_only_its_slashes_are_stripped() -> None:
    server = Server()
    connect.Connect(server, "https://connect.exampleXX//", "t").call(
        "GET", "/v1/x", "x"
    )
    assert server.calls[0][1] == "https://connect.exampleXX/v1/x"


def failing_at(server: Server, method: str, tail: str) -> connect.Connect:
    """A client whose call to METHOD on a path ending in TAIL answers 500."""

    def http(
        verb: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        if verb == method and url.endswith(tail):
            return 500, b""
        return server(verb, url, headers, body)

    return connect.Connect(http, HOST, "t")


def test_each_call_names_itself_when_it_fails() -> None:
    ref = "op:\x2f/host-0-shared/scanner-policy/config"
    with pytest.raises(CliError) as raised:
        connect.vaults(failing_at(Server(), "GET", "/v1/vaults"))
    assert str(raised.value) == "listing the vaults failed: HTTP 500"
    messages: list[str] = []
    cases: list[tuple[str, str, list[dict[str, object]]]] = [
        ("GET", "items?filter=title%20eq%20%22scanner-policy%22", []),
        ("POST", "/items", []),
        ("GET", "/items/item-1", [item("item-1")]),
    ]
    for method, tail, items in cases:
        server = Server(items)
        with pytest.raises(CliError) as raised:
            connect.publish(failing_at(server, method, tail), VAULT, "NEW", write=True)
        messages.append(str(raised.value))
    assert messages == [
        "listing items failed: HTTP 500",
        f"creating {ref} failed: HTTP 500",
        f"reading {ref} failed: HTTP 500",
    ]


def test_a_token_that_reaches_no_vault_lists_none_without_an_error() -> None:
    server = Server()
    server.raw = b"[]"
    assert connect.listed_vaults(client(server)) == {}


def test_publish_uses_a_configured_item_reference() -> None:
    ref = connect.ItemRef("vault-a", "item-a", "field-a")
    server = Server()
    assert connect.publish(client(server), VAULT, "NEW", write=True, ref=ref) == (
        "created"
    )
    list_call, create_call = server.calls
    assert list_call[:2] == (
        "GET",
        "https://connect.example/v1/vaults/vault-id/items?filter=title%20eq%20%22item-a%22",
    )
    assert create_call[3] == {
        "vault": {"id": VAULT},
        "title": "item-a",
        "category": "SECURE_NOTE",
        "fields": [
            {"id": "field-a", "label": "field-a", "type": "CONCEALED", "value": "NEW"}
        ],
    }
    duplicate = Server(
        [
            {"id": "a", "title": "item-a", "vault": {"id": VAULT}, "fields": []},
            {"id": "b", "title": "item-a", "vault": {"id": VAULT}, "fields": []},
        ]
    )
    with pytest.raises(CliError) as raised:
        connect.publish(client(duplicate), VAULT, "NEW", write=True, ref=ref)
    assert str(raised.value) == "vault-a holds 2 items named item-a"
    with pytest.raises(CliError) as raised:
        connect.publish(
            failing_at(Server(), "POST", "/items"), VAULT, "NEW", write=True, ref=ref
        )
    assert (
        str(raised.value) == "creating op:\x2f/vault-a/item-a/field-a failed: HTTP 500"
    )
