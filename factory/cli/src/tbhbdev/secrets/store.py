"""Read secret references and manage items by vault and item name or ID."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

from tbhbdev.secrets import config
from tbhbdev.secrets.connect import Connect, SecretsError, objects

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tbhbdev.secrets.connect import Json

REF_PARTS = 3

# Connect assigns these when it stores an item; a copy for another vault
# leaves them out.
_ASSIGNED = frozenset(
    {"id", "vault", "version", "createdAt", "updatedAt", "lastEditedBy"}
)


class Backend(Protocol):
    """Where items live. Connect is the one backend today.

    Vaults and items are Connect's JSON objects, so an edit keeps every field
    of the item it doesn't touch.
    """

    def vaults(self) -> list[Json]: ...
    def items(self, vault_id: str) -> list[Json]: ...
    def item(self, vault_id: str, item_id: str) -> Json: ...
    def create(self, vault_id: str, item: Json) -> Json: ...
    def update(self, vault_id: str, item_id: str, item: Json) -> Json: ...
    def delete(self, vault_id: str, item_id: str) -> None: ...


def connect() -> Connect:
    """Connect with this user's token, at the configured endpoints."""
    return Connect(config.token(), config.endpoints())


@dataclass(frozen=True)
class Field:
    """One field of an item. Its value stays out of repr."""

    id: str
    label: str
    type: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    category: str
    vault_id: str
    vault: str
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class Summary:
    id: str
    title: str
    category: str
    vault: str


def parse_ref(ref: str) -> tuple[str, str, str]:
    """Split `vault/item/field` or `op://vault/item/field`.

    Strip the optional `?ssh-format=openssh` query and refuse any other query.

    Raises:
        SecretsError: REF has another shape.
    """
    path, separator, query = ref.partition("?")
    if separator and query != "ssh-format=openssh":
        message = f"{ref!r} has an unsupported reference query"
        raise SecretsError(message)
    parts = path.removeprefix("op://").split("/")
    if len(parts) != REF_PARTS or not all(parts):
        message = (
            f"{ref!r} is not a vault/item/field or op://vault/item/field reference"
        )
        raise SecretsError(message)
    return parts[0], parts[1], parts[2]


def _openssh(value: str, label: str) -> str:
    """Convert a Connect PEM value in memory without exposing conversion errors."""
    try:
        key = serialization.load_pem_private_key(value.encode(), password=None)
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
    except ValueError, TypeError, UnsupportedAlgorithm:
        message = f"field {label!r} cannot convert to OpenSSH private key"
        raise SecretsError(message) from None


def _text(value: Json, key: str) -> str:
    entry = value.get(key)
    return entry if isinstance(entry, str) else ""


def _fields(raw: Json) -> list[Json]:
    entries = raw.get("fields")
    return objects(entries) if entries is not None else []


def _one(matches: list[Json], what: str) -> Json:
    if not matches:
        message = f"no {what}"
        raise SecretsError(message)
    if len(matches) > 1:
        message = f"more than one {what}"
        raise SecretsError(message)
    return matches[0]


def _new_field(label: str, value: str) -> Json:
    return {"label": label, "value": value, "type": "CONCEALED"}


class Store:
    """Items in the vaults the token can reach, by vault and item name or ID."""

    def __init__(self, backend: Backend | None = None) -> None:
        self._backend = backend or connect()

    def _vault(self, ref: str) -> tuple[str, str]:
        vaults = self._backend.vaults()
        match = _one(
            [v for v in vaults if ref in {_text(v, "id"), _text(v, "name")}],
            f"vault {ref!r}",
        )
        return _text(match, "id"), _text(match, "name")

    def _titled(self, vault_id: str, ref: str) -> list[Json]:
        return [
            entry
            for entry in self._backend.items(vault_id)
            if ref in {_text(entry, "id"), _text(entry, "title")}
        ]

    def _item_id(self, vault_id: str, vault: str, ref: str) -> str:
        match = _one(self._titled(vault_id, ref), f"item {ref!r} in vault {vault!r}")
        return _text(match, "id")

    def _absent(self, vault_id: str, vault: str, title: str) -> None:
        if self._titled(vault_id, title):
            message = f"vault {vault!r} already has an item titled {title!r}"
            raise SecretsError(message)

    def _item(self, vault_id: str, vault: str, raw: Json) -> Item:
        fields = tuple(
            Field(
                _text(entry, "id"),
                _text(entry, "label"),
                _text(entry, "type"),
                _text(entry, "value"),
            )
            for entry in _fields(raw)
        )
        return Item(
            _text(raw, "id"),
            _text(raw, "title"),
            _text(raw, "category"),
            vault_id,
            vault,
            fields,
        )

    def read(self, ref: str) -> str:
        """The value of the field a `vault/item/field` reference names."""
        vault_ref, item_ref, field_ref = parse_ref(ref)
        vault_id, vault = self._vault(vault_ref)
        raw = self._backend.item(vault_id, self._item_id(vault_id, vault, item_ref))
        match = _one(
            [
                f
                for f in _fields(raw)
                if field_ref in {_text(f, "id"), _text(f, "label")}
            ],
            f"field {field_ref!r} in item {item_ref!r}",
        )
        value = _text(match, "value")
        return _openssh(value, field_ref) if "?" in ref else value

    def get(self, vault: str, item: str) -> Item:
        vault_id, name = self._vault(vault)
        raw = self._backend.item(vault_id, self._item_id(vault_id, name, item))
        return self._item(vault_id, name, raw)

    def list_items(self, vault: str | None = None) -> list[Summary]:
        """The items in VAULT, or in every vault the token can reach."""
        if vault is None:
            vaults = [
                (_text(v, "id"), _text(v, "name")) for v in self._backend.vaults()
            ]
        else:
            vaults = [self._vault(vault)]
        return [
            Summary(
                _text(entry, "id"),
                _text(entry, "title"),
                _text(entry, "category"),
                name,
            )
            for vault_id, name in vaults
            for entry in self._backend.items(vault_id)
        ]

    def create(self, vault: str, title: str, fields: Mapping[str, str]) -> Item:
        """A new secure note titled TITLE, with one concealed field per entry."""
        vault_id, name = self._vault(vault)
        self._absent(vault_id, name, title)
        body: Json = {
            "title": title,
            "category": "SECURE_NOTE",
            "vault": {"id": vault_id},
            "fields": [_new_field(label, value) for label, value in fields.items()],
        }
        return self._item(vault_id, name, self._backend.create(vault_id, body))

    def edit(self, vault: str, item: str, fields: Mapping[str, str]) -> Item:
        """Set each labeled field's value; a new label adds a concealed field."""
        vault_id, name = self._vault(vault)
        item_id = self._item_id(vault_id, name, item)
        raw = self._backend.item(vault_id, item_id)
        entries = _fields(raw)
        for label, value in fields.items():
            labeled = [f for f in entries if _text(f, "label") == label]
            if len(labeled) > 1:
                message = f"item {item!r} has more than one field labeled {label!r}"
                raise SecretsError(message)
            if labeled:
                labeled[0]["value"] = value
            else:
                entries.append(_new_field(label, value))
        raw["fields"] = entries
        return self._item(vault_id, name, self._backend.update(vault_id, item_id, raw))

    def delete(self, vault: str, item: str) -> None:
        vault_id, name = self._vault(vault)
        self._backend.delete(vault_id, self._item_id(vault_id, name, item))

    def move(self, vault: str, item: str, destination: str) -> Item:
        """Create a copy of ITEM in DESTINATION, then delete the original.

        When either step fails, the copy is deleted again, so the item stays
        in VAULT and nothing is left behind in DESTINATION. A failed delete
        that removed the item anyway still counts as a move. When the move
        can't tell whether the delete happened, it keeps the copy.

        Raises:
            SecretsError: The move failed. The message says whether a copy
                could remain.
        """
        src_id, src = self._vault(vault)
        dst_id, dst = self._vault(destination)
        item_id = self._item_id(src_id, src, item)
        raw = self._backend.item(src_id, item_id)
        if raw.get("files"):
            message = f"item {item!r} has files, which Connect can't copy"
            raise SecretsError(message)
        title = _text(raw, "title")
        self._absent(dst_id, dst, title)
        copy = {key: value for key, value in raw.items() if key not in _ASSIGNED}
        copy["vault"] = {"id": dst_id}
        try:
            created = self._backend.create(dst_id, copy)
        except SecretsError:
            self._discard(dst_id, dst, title)
            raise
        try:
            self._backend.delete(src_id, item_id)
        except SecretsError:
            # A delete can fail after Connect has done it, and the copy is
            # then the only one left.
            try:
                # By id alone: another item may have this id as its title.
                listed = self._backend.items(src_id)
                kept = any(_text(entry, "id") == item_id for entry in listed)
            except SecretsError as exc:
                message = (
                    f"the move couldn't tell whether it deleted item {item!r}"
                    f" from vault {vault!r}, so it kept the copy in vault"
                    f" {destination!r}"
                )
                raise SecretsError(message) from exc
            if kept:
                self._discard(dst_id, dst, title)
                raise
        return self._item(dst_id, dst, created)

    def _discard(self, vault_id: str, vault: str, title: str) -> None:
        """Delete every item titled TITLE: the copy a failed move made.

        Connect may store a copy even when the request that made it fails. The
        destination had no item with this title before the move, so any there
        now is the move's.
        """
        try:
            for entry in self._titled(vault_id, title):
                self._backend.delete(vault_id, _text(entry, "id"))
        except SecretsError as exc:
            message = f"the move failed, and its copy may remain in vault {vault!r}"
            raise SecretsError(message) from exc


def read(ref: str) -> str:
    """The value a `vault/item/field` or `op://vault/item/field` reference names."""
    return Store().read(ref)
