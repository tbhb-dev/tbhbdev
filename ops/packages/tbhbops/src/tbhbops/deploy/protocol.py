"""Release stream validation, independent of privileged host operations."""

import hashlib
import io
import json
import posixpath
import re
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

MEMBERS = 10_000
PATH_BYTES = 255
REQUEST = re.compile(r"([a-z][a-z0-9-]{0,31}) ([0-9a-f]{40})(?: ([0-9]{1,20}))?")
type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


class RefusalError(ValueError):
    """A safe reason that contains neither secret bytes nor command output."""


def require(condition: bool, reason: str) -> None:  # noqa: FBT001  # predicate assertion API
    if not condition:
        raise RefusalError(reason)


def mapping(value: Json, keys: set[str] | None = None) -> dict[str, Json]:
    require(isinstance(value, dict), "expected an object")
    result = cast(
        # typing.cast ignores its type argument at runtime.
        "dict[str, Json]",  # pragma: no mutate
        value,
    )
    require(keys is None or result.keys() <= keys, "unknown object key")
    return result


def array(value: Json) -> list[Json]:
    require(isinstance(value, list), "expected array")
    return cast(
        # typing.cast ignores its type argument at runtime.
        "list[Json]",  # pragma: no mutate
        value,
    )


def strings(value: Json) -> list[str]:
    require(isinstance(value, list), "expected a string list")
    result = cast(
        # typing.cast ignores its type argument at runtime.
        "list[Json]",  # pragma: no mutate
        value,
    )
    require(all(isinstance(item, str) for item in result), "expected strings")
    return cast(
        # typing.cast ignores its type argument at runtime.
        "list[str]",  # pragma: no mutate
        result,
    )


def unique(pairs: list[tuple[str, Json]]) -> dict[str, Json]:
    result: dict[str, Json] = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def decode(data: bytes) -> Json:
    try:
        return cast(
            # typing.cast ignores its type argument at runtime.
            "Json",  # pragma: no mutate
            json.loads(data, object_pairs_hook=unique),
        )
    except ValueError, UnicodeError:
        message = "invalid JSON"
        raise RefusalError(message) from None


def relative(path: str) -> str:
    require(bool(path) and len(path.encode()) <= PATH_BYTES, "invalid member path")
    require(not path.startswith("/"), "absolute member path")
    require(".." not in PurePosixPath(path).parts, "parent member path")
    result = posixpath.normpath(path)
    require(result != ".", "empty member path")
    return result


@dataclass(frozen=True)
class Request:
    service: str
    release: str
    run_id: str | None


def request(value: str) -> Request:
    match = REQUEST.fullmatch(value)
    require(match is not None, "invalid request")
    return Request(
        *cast(
            # typing.cast ignores its type argument at runtime.
            "re.Match[str]",  # pragma: no mutate
            match,
        ).groups()
    )


@dataclass(frozen=True)
class Release:
    files: dict[str, bytes]
    secrets: dict[str, bytes]
    digest: str


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(value: Json, wanted: Request) -> dict[str, Json]:
    model = mapping(value, {"format", "service", "release", "files", "secrets"})
    require(type(model.get("format")) is int and model["format"] == 1, "stream format")
    require(model.get("service") == wanted.service, "stream service")
    require(model.get("release") == wanted.release, "stream release")
    return model


def member_path(member: tarfile.TarInfo, count: int, seen: set[str], limit: int) -> str:
    require(count <= MEMBERS, "member limit")
    path = relative(member.name)
    require(path not in seen, "duplicate member")
    seen.add(path)
    require(member.isfile() or member.isdir(), "member type")
    require(member.sparse is None, "sparse member")
    require(member.pax_headers.keys() <= {"path", "size"}, "pax header")
    require(0 <= member.size <= limit, "file limit")
    if count == 1:
        require(path == "manifest.json" and member.isfile(), "manifest first")
    else:
        require(path.split("/")[0] in {"release", "secrets"}, "member location")
        require("/" in path or member.isdir(), "namespace must be a directory")
    return path


def unpack(data: bytes, wanted: Request, names: set[str], limit: int) -> Release:
    """Validate the whole bounded stream before a caller writes any member."""
    require(len(data) <= limit, "stream limit")
    seen: set[str] = set()
    files: dict[str, bytes] = {}
    secrets: dict[str, bytes] = {}
    manifest: dict[str, Json] | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for count, member in enumerate(archive, 1):
                path = member_path(member, count, seen, limit)
                if member.isdir():
                    continue
                source = archive.extractfile(member)
                require(source is not None, "missing member body")
                body = cast(
                    # typing.cast ignores its type argument at runtime.
                    "tarfile.ExFileObject",  # pragma: no mutate
                    source,
                ).read(limit + 1)
                require(len(body) == member.size, "member size")
                if count == 1:
                    manifest = _manifest(decode(body), wanted)
                elif path.startswith("release/"):
                    files[path] = body
                else:
                    name = path.removeprefix("secrets/")
                    require(name in names, "unlisted secret")
                    secrets[name] = body
    except RefusalError:
        raise
    except Exception:  # noqa: BLE001  # tarfile input failures share a refusal boundary.
        # tarfile can raise RecursionError and other non-TarError exceptions
        # while interpreting attacker-controlled extension headers.
        message = "invalid tar stream"
        raise RefusalError(message) from None
    require(manifest is not None, "missing manifest")
    manifest = cast(
        # typing.cast ignores its type argument at runtime.
        "dict[str, Json]",  # pragma: no mutate
        manifest,
    )
    require(set(secrets) == names, "missing secret")
    actual_files = {path: sha256(body) for path, body in files.items()}
    actual_secrets = {name: sha256(body) for name, body in secrets.items()}
    require(manifest.get("files") == actual_files, "file manifest mismatch")
    require(manifest.get("secrets") == actual_secrets, "secret manifest mismatch")
    require(data[archive.offset :] == bytes(tarfile.BLOCKSIZE * 2), "tar end framing")
    digest = sha256(json.dumps(actual_files, sort_keys=True).encode())
    return Release(files, secrets, digest)


def pack(wanted: Request, files: dict[str, bytes], secrets: dict[str, bytes]) -> bytes:
    manifest = {
        "format": 1,
        "service": wanted.service,
        "release": wanted.release,
        "files": {name: sha256(body) for name, body in files.items()},
        "secrets": {name: sha256(body) for name, body in secrets.items()},
    }
    members = {"manifest.json": json.dumps(manifest, sort_keys=True).encode()}
    members.update(files)
    members.update({f"secrets/{name}": body for name, body in secrets.items()})
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, body in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(body))
    result = output.getvalue()[: archive.offset]
    unpack(result, wanted, set(secrets), len(result))
    return result
