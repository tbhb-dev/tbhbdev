"""Check secret references and item operations against a fake Connect server."""

from __future__ import annotations

import dataclasses
import functools
import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_host_example import host_settings, run_request
from conftest import SECRETS_FIXTURE_VALUE
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa, x25519

from tbhbdev.alpha.agent import credentials
from tbhbdev.secrets import store as secret_store
from tbhbdev.secrets.config import TEST_TOKEN_ENV
from tbhbdev.secrets.connect import Connect, SecretsError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conftest import FakeConnect, Json


def store(secrets_server: FakeConnect) -> secret_store.Store:
    return secret_store.Store(Connect(secrets_server.token, [secrets_server.url]))


@pytest.mark.parametrize(
    ("ref", "parts"),
    [
        ("ops/db/password", ("ops", "db", "password")),
        ("op://ops/db/password", ("ops", "db", "password")),
    ],
)
def test_parse_ref_takes_both_forms(ref: str, parts: tuple[str, str, str]) -> None:
    assert secret_store.parse_ref(ref) == parts


@pytest.mark.parametrize(
    "ref", ["ops/db", "ops/db/section/password", "ops//password", "op://", ""]
)
def test_parse_ref_rejects_other_shapes(ref: str) -> None:
    with pytest.raises(SecretsError, match="not a vault/item/field"):
        secret_store.parse_ref(ref)


@pytest.mark.parametrize(
    "ref", ["ops/db/password", "op://ops/db/password", "v1/i1/f0", "op://v1/db/f0"]
)
def test_read_by_name_or_id(secrets_server: FakeConnect, ref: str) -> None:
    assert store(secrets_server).read(ref) == SECRETS_FIXTURE_VALUE


def test_read_a_field_without_a_value_is_empty(secrets_server: FakeConnect) -> None:
    fields = secrets_server.items["v1"]["i1"]["fields"]
    assert isinstance(fields, list)
    fields.append({"id": "f9", "label": "empty", "type": "STRING"})
    assert store(secrets_server).read("ops/db/empty") == ""


def test_read_an_item_without_fields_fails(secrets_server: FakeConnect) -> None:
    del secrets_server.items["v1"]["i1"]["fields"]
    with pytest.raises(SecretsError, match="no field 'password'"):
        store(secrets_server).read("ops/db/password")


@pytest.mark.parametrize(
    ("ref", "error"),
    [
        ("nope/db/password", "no vault 'nope'"),
        ("ops/nope/password", "no item 'nope' in vault 'ops'"),
        ("ops/db/nope", "no field 'nope' in item 'db'"),
    ],
)
def test_read_something_missing_fails(
    secrets_server: FakeConnect, ref: str, error: str
) -> None:
    with pytest.raises(SecretsError, match=error) as caught:
        store(secrets_server).read(ref)
    assert SECRETS_FIXTURE_VALUE not in str(caught.value)


def test_read_an_ambiguous_title_fails(secrets_server: FakeConnect) -> None:
    secrets_server.item("v1", "db", password="other")  # noqa: S106
    with pytest.raises(SecretsError, match="more than one item 'db'"):
        store(secrets_server).read("ops/db/password")


def test_module_read_uses_the_keychain_token_and_config(
    secrets_server: FakeConnect,
    config: Callable[[list[str]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config([secrets_server.url])
    monkeypatch.setenv(TEST_TOKEN_ENV, secrets_server.token)
    assert secret_store.read("ops/db/password") == SECRETS_FIXTURE_VALUE


def test_get_returns_metadata_and_fields_without_values_in_repr(
    secrets_server: FakeConnect,
) -> None:
    item = store(secrets_server).get("ops", "db")
    assert (item.id, item.title, item.category, item.vault_id, item.vault) == (
        "i1",
        "db",
        "LOGIN",
        "v1",
        "ops",
    )
    assert [(f.id, f.label, f.type, f.value) for f in item.fields] == [
        ("f0", "password", "CONCEALED", SECRETS_FIXTURE_VALUE),
        ("f1", "user", "CONCEALED", "admin"),
    ]
    assert SECRETS_FIXTURE_VALUE not in repr(item)


def test_list_items_in_one_vault_or_all(secrets_server: FakeConnect) -> None:
    secrets_server.item("v2", "spare-key", key="k")
    one = store(secrets_server).list_items("ops")
    assert [(s.vault, s.title) for s in one] == [("ops", "db")]
    every = store(secrets_server).list_items()
    assert [(s.vault, s.title, s.id) for s in every] == [
        ("ops", "db", "i1"),
        ("spare", "spare-key", "i2"),
    ]


# Writing.


def test_create_makes_a_secure_note_of_concealed_fields(
    secrets_server: FakeConnect,
) -> None:
    item = store(secrets_server).create("spare", "api", {"key": "k1", "secret": "k2"})
    stored = secrets_server.items["v2"][item.id]
    assert stored["category"] == "SECURE_NOTE"
    assert stored["vault"] == {"id": "v2"}
    assert stored["fields"] == [
        {"label": "key", "value": "k1", "type": "CONCEALED"},
        {"label": "secret", "value": "k2", "type": "CONCEALED"},
    ]
    assert (item.vault_id, item.vault) == ("v2", "spare")


def test_create_refuses_a_title_the_vault_has(secrets_server: FakeConnect) -> None:
    with pytest.raises(SecretsError, match="vault 'ops' already has an item"):
        store(secrets_server).create("ops", "db", {"x": "y"})


def test_edit_sets_existing_fields_adds_new_ones_and_keeps_the_rest(
    secrets_server: FakeConnect,
) -> None:
    secrets_server.items["v1"]["i1"]["urls"] = [{"href": "https://db"}]
    item = store(secrets_server).edit("ops", "db", {"password": "new", "port": "5432"})
    assert (item.vault_id, item.vault) == ("v1", "ops")
    stored = secrets_server.items["v1"]["i1"]
    assert stored["urls"] == [{"href": "https://db"}]
    assert stored["fields"] == [
        {"id": "f0", "label": "password", "type": "CONCEALED", "value": "new"},
        {"id": "f1", "label": "user", "type": "CONCEALED", "value": "admin"},
        {"label": "port", "value": "5432", "type": "CONCEALED"},
    ]


def test_edit_an_ambiguous_label_fails_without_writing(
    secrets_server: FakeConnect,
) -> None:
    fields = secrets_server.items["v1"]["i1"]["fields"]
    assert isinstance(fields, list)
    fields.append({"id": "f9", "label": "password", "type": "CONCEALED", "value": "x"})
    with pytest.raises(SecretsError, match="more than one field labeled"):
        store(secrets_server).edit("ops", "db", {"password": "new"})
    assert ("PUT", "/v1/vaults/v1/items/i1") not in secrets_server.requests


def test_delete_removes_the_item(secrets_server: FakeConnect) -> None:
    store(secrets_server).delete("ops", "db")
    assert secrets_server.items["v1"] == {}


@pytest.mark.parametrize(
    "call",
    [
        functools.partial(secret_store.Store.get, vault="ops", item="nope"),
        functools.partial(secret_store.Store.edit, vault="ops", item="nope", fields={}),
        functools.partial(secret_store.Store.delete, vault="ops", item="nope"),
        functools.partial(
            secret_store.Store.move, vault="ops", item="nope", destination="spare"
        ),
    ],
)
def test_a_missing_item_names_its_vault(
    secrets_server: FakeConnect, call: Callable[[secret_store.Store], object]
) -> None:
    with pytest.raises(SecretsError) as caught:
        call(store(secrets_server))
    assert str(caught.value) == "no item 'nope' in vault 'ops'"


# Moving.


def test_move_copies_then_deletes(secrets_server: FakeConnect) -> None:
    secrets_server.items["v1"]["i1"]["tags"] = ["db"]
    moved = store(secrets_server).move("ops", "db", "spare")
    assert secrets_server.items["v1"] == {}
    copy = secrets_server.items["v2"][moved.id]
    assert copy["vault"] == {"id": "v2"}
    assert copy["tags"] == ["db"]
    assert "version" not in copy
    assert (moved.vault_id, moved.vault) == ("v2", "spare")
    assert [f.value for f in moved.fields] == [SECRETS_FIXTURE_VALUE, "admin"]


@pytest.mark.parametrize(
    ("fail", "fail_after"),
    [
        ({"POST": 500}, {}),
        ({"POST": 400}, {}),
        ({}, {"POST": 500}),
        ({}, {"POST": 409}),
        ({"DELETE /v1/vaults/v1/items/i1": 403}, {}),
        ({"DELETE /v1/vaults/v1/items/i1": 503}, {}),
    ],
)
def test_a_failed_move_leaves_nothing_behind(
    secrets_server: FakeConnect, fail: dict[str, int], fail_after: dict[str, int]
) -> None:
    before = json.dumps(secrets_server.items["v1"])
    secrets_server.fail, secrets_server.fail_after = fail, fail_after
    with pytest.raises(SecretsError) as caught:
        store(secrets_server).move("ops", "db", "spare")
    assert secrets_server.items["v2"] == {}
    assert json.dumps(secrets_server.items["v1"]) == before
    assert SECRETS_FIXTURE_VALUE not in str(caught.value)


def test_a_delete_that_fails_after_deleting_keeps_the_copy(
    secrets_server: FakeConnect,
) -> None:
    secrets_server.fail_after = {"DELETE": 500}
    moved = store(secrets_server).move("ops", "db", "spare")
    assert secrets_server.items["v1"] == {}
    assert secrets_server.items["v2"][moved.id]["title"] == "db"


def test_a_title_like_the_items_id_does_not_count_as_the_item(
    secrets_server: FakeConnect,
) -> None:
    secrets_server.item("v1", "i1")
    secrets_server.fail_after = {"DELETE": 500}
    moved = store(secrets_server).move("ops", "db", "spare")
    assert "i1" not in secrets_server.items["v1"]
    assert secrets_server.items["v2"][moved.id]["title"] == "db"


def test_a_failed_check_after_a_failed_delete_keeps_the_copy(
    secrets_server: FakeConnect,
) -> None:
    secrets_server.fail = {"DELETE": 503}
    real = secrets_server.handle

    def handle(method: str, path: str, auth: str, body: Json | None) -> object:
        # The check after the failed delete is the first GET of vault v1's items
        # that follows a DELETE.
        if (method, path) == ("GET", "/v1/vaults/v1/items") and any(
            m == "DELETE" for m, _ in secrets_server.requests
        ):
            secrets_server.requests.append((method, path))
            return 503
        return real(method, path, auth, body)

    secrets_server.handle = handle  # type: ignore[method-assign]
    with pytest.raises(SecretsError) as caught:
        store(secrets_server).move("ops", "db", "spare")
    assert str(caught.value) == (
        "the move couldn't tell whether it deleted item 'db' from vault 'ops',"
        " so it kept the copy in vault 'spare'"
    )
    assert secrets_server.items["v1"]["i1"]["title"] == "db"
    assert [i["title"] for i in secrets_server.items["v2"].values()] == ["db"]


def test_a_failed_cleanup_says_a_copy_may_remain(secrets_server: FakeConnect) -> None:
    secrets_server.fail_after = {"POST": 500}
    secrets_server.fail = {"DELETE": 403}
    with pytest.raises(SecretsError, match="may remain in vault 'spare'"):
        store(secrets_server).move("ops", "db", "spare")
    assert secrets_server.items["v1"]["i1"]["title"] == "db"


def test_a_failed_cleanup_after_a_failed_delete_says_a_copy_may_remain(
    secrets_server: FakeConnect,
) -> None:
    secrets_server.fail = {"DELETE": 403}
    with pytest.raises(SecretsError, match="may remain in vault 'spare'"):
        store(secrets_server).move("ops", "db", "spare")
    assert secrets_server.items["v1"]["i1"]["title"] == "db"
    assert [i["title"] for i in secrets_server.items["v2"].values()] == ["db"]


def test_move_refuses_an_item_with_files(secrets_server: FakeConnect) -> None:
    secrets_server.items["v1"]["i1"]["files"] = [{"id": "file1"}]
    with pytest.raises(SecretsError, match="has files"):
        store(secrets_server).move("ops", "db", "spare")
    assert secrets_server.items["v2"] == {}


def test_move_refuses_a_title_the_destination_has(secrets_server: FakeConnect) -> None:
    secrets_server.item("v2", "db", password="other")  # noqa: S106
    with pytest.raises(SecretsError, match="vault 'spare' already has an item"):
        store(secrets_server).move("ops", "db", "spare")
    assert "i1" in secrets_server.items["v1"]


@pytest.mark.parametrize("prefix", ["", "op://"])
def test_parse_ref_strips_openssh_query(prefix: str) -> None:
    assert secret_store.parse_ref(
        f"{prefix}vault/item/private key?ssh-format=openssh"
    ) == (
        "vault",
        "item",
        "private key",
    )


@pytest.mark.parametrize(
    "query", ["ssh-format=pkcs8", "attribute=otp", "ssh-format=openssh&x=1", ""]
)
def test_reference_queries_are_exact(secrets_server: FakeConnect, query: str) -> None:
    reference = f"ops/db/password?{query}"
    with pytest.raises(SecretsError) as caught:
        store(secrets_server).read(reference)
    assert str(caught.value) == f"{reference!r} has an unsupported reference query"


@pytest.fixture
def signing_pem(secrets_server: FakeConnect) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    secrets_server.item("v1", "signing", **{"private key": pem.decode()})
    return pem


@pytest.mark.parametrize("kind", ["ed25519", "rsa"])
@pytest.mark.parametrize("prefix", ["", "op://"])
def test_read_converts_signing_keys(
    secrets_server: FakeConnect, kind: str, prefix: str
) -> None:
    key = (
        ed25519.Ed25519PrivateKey.generate()
        if kind == "ed25519"
        else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    secrets_server.item("v1", "signing", **{"private key": pem})
    reader = store(secrets_server)
    assert reader.read("ops/signing/private key") == pem
    converted = reader.read(f"{prefix}ops/i2/private key?ssh-format=openssh")
    assert converted.startswith("-----BEGIN " + "OPENSSH PRIVATE KEY-----\n")
    loaded = serialization.load_ssh_private_key(converted.encode(), password=None)
    assert loaded.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ) == key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )


@pytest.mark.parametrize("kind", ["encrypted", "malformed", "unsupported"])
def test_read_refuses_invalid_signing_keys(
    secrets_server: FakeConnect, signing_pem: bytes, kind: str
) -> None:
    if kind == "malformed":
        pem = signing_pem.replace(b"PRIVATE KEY", b"BROKEN KEY")
    else:
        key = (
            ed25519.Ed25519PrivateKey.generate()
            if kind == "encrypted"
            else x25519.X25519PrivateKey.generate()
        )
        encryption = (
            serialization.BestAvailableEncryption(b"example-passphrase")
            if kind == "encrypted"
            else serialization.NoEncryption()
        )
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
        )
    secrets_server.item("v1", "refused", **{"private key": pem.decode()})
    with pytest.raises(SecretsError) as caught:
        store(secrets_server).read("ops/refused/private key?ssh-format=openssh")
    assert (
        str(caught.value) == "field 'private key' cannot convert to OpenSSH private key"
    )
    assert not any(line in str(caught.value) for line in pem.decode().splitlines())
    assert caught.value.__suppress_context__


def test_converted_key_reaches_only_ssh_add_stdin(
    secrets_server: FakeConnect,
    signing_pem: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        secret_store,
        "connect",
        lambda: Connect(secrets_server.token, [secrets_server.url]),
    )
    settings = host_settings()
    refs = dataclasses.replace(
        settings.credentials[0],
        gh="ops/db/password",
        docker=None,
        signing="ops/signing/private key?ssh-format=openssh",
    )
    settings = dataclasses.replace(settings, credentials=(refs,))
    delivery = credentials.deliver(run_request(), settings, tmp_path)
    assert delivery.signing is not None
    assert delivery.signing.startswith("-----BEGIN " + "OPENSSH PRIVATE KEY-----\n")
    assert delivery.signing not in repr(delivery)
    agent = Mock(spec=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=agent))
    monkeypatch.setattr(credentials, "agent_ready", Mock())
    add = Mock()
    monkeypatch.setattr(subprocess, "run", add)
    assert credentials.signing_agent(delivery.signing, tmp_path, 10) is agent
    assert add.call_args.args == ([credentials.host_tools.resolve("ssh-add"), "-"],)
    assert add.call_args.kwargs["input"] == delivery.signing
    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert delivery.signing.encode() not in content
            assert signing_pem not in content


def test_read_hides_unsupported_algorithm_errors(
    secrets_server: FakeConnect, signing_pem: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_data: bytes, password: bytes | None) -> None:
        assert password is None
        raise UnsupportedAlgorithm(signing_pem.decode())

    monkeypatch.setattr(serialization, "load_pem_private_key", fail)
    with pytest.raises(SecretsError) as caught:
        store(secrets_server).read("ops/signing/private key?ssh-format=openssh")
    assert (
        str(caught.value) == "field 'private key' cannot convert to OpenSSH private key"
    )
    assert caught.value.__suppress_context__
