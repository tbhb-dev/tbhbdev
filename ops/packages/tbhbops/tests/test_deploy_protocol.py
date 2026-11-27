"""The receive wire format refuses ambiguous or unbounded archives."""

import gzip
import io
import json
import tarfile
from unittest.mock import Mock, patch

import pytest

from tbhbops.deploy import protocol as wire

REQUEST = wire.Request("o11y", "a" * 40, "123")


def archive(members: list[tuple[str, bytes, bytes, dict[str, str]]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as stream:
        for name, body, kind, headers in members:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(body)
            member.pax_headers = headers
            stream.addfile(member, io.BytesIO(body))
    return output.getvalue()[: stream.offset]


def manifest(**changes: wire.Json) -> bytes:
    model: dict[str, wire.Json] = {
        "format": 1,
        "service": "o11y",
        "release": "a" * 40,
        "files": {"release/config": wire.sha256(b"config")},
        "secrets": {"token": wire.sha256(b"fixture")},
    }
    model.update(changes)
    return json.dumps(model).encode()


def members() -> list[tuple[str, bytes, bytes, dict[str, str]]]:
    return [
        ("manifest.json", manifest(), tarfile.REGTYPE, {}),
        ("release/config", b"config", tarfile.REGTYPE, {}),
        ("secrets/token", b"fixture", tarfile.REGTYPE, {}),
    ]


def test_round_trip() -> None:
    files = {"release/config": b"config"}
    secrets = {"token": b"fixture"}
    stream = wire.pack(REQUEST, files, secrets)
    result = wire.unpack(stream, REQUEST, {"token"}, len(stream))
    assert result.files == files
    assert result.secrets == secrets
    assert result.digest == wire.sha256(
        json.dumps({"release/config": wire.sha256(b"config")}, sort_keys=True).encode()
    )
    assert wire.request("o11y " + "a" * 40 + " 123") == REQUEST
    assert wire.request("o11y " + "a" * 40).run_id is None


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "invalid request"),
        ("o11y aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", "invalid request"),
        ("o11y  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "invalid request"),
        ("-x aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "invalid request"),
        ("o11y AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "invalid request"),
        ("o11y aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1 2", "invalid request"),
        (
            "o11y aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 111111111111111111111",
            "invalid request",
        ),
    ],
)
def test_request_refuses(value: str, reason: str) -> None:
    with pytest.raises(wire.RefusalError) as caught:
        wire.request(value)
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (b"{", "invalid JSON"),
        (b"\xff", "invalid JSON"),
        (b'{"x": 1, "x": 2}', "invalid JSON"),
        (b'{"nested":{"x":0,"x":1}}', "invalid JSON"),
    ],
)
def test_invalid_json(value: bytes, reason: str) -> None:
    with pytest.raises(wire.RefusalError) as caught:
        wire.decode(value)
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "invalid member path"),
        ("/absolute", "absolute member path"),
        ("x/../y", "parent member path"),
        (
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "invalid member path",
        ),
        (".", "empty member path"),
    ],
)
def test_relative_refuses(value: str, reason: str) -> None:
    with pytest.raises(wire.RefusalError) as caught:
        wire.relative(value)
    assert str(caught.value) == reason


def test_json_schema_helpers() -> None:
    assert wire.mapping({"x": 1}) == {"x": 1}
    assert wire.mapping({}, {"x"}) == {}
    assert wire.strings(["x"]) == ["x"]
    assert wire.array([]) == []
    assert wire.decode(b'{"x": 1}') == {"x": 1}
    invalid_objects: list[wire.Json] = [None, [], "x", 1]
    for value in invalid_objects:
        with pytest.raises(wire.RefusalError):
            wire.mapping(value)
    with pytest.raises(wire.RefusalError):
        wire.mapping({"y": 1}, {"x"})
    invalid_lists: list[wire.Json] = [None, [1]]
    for value in invalid_lists:
        with pytest.raises(wire.RefusalError):
            wire.strings(value)
    with pytest.raises(wire.RefusalError):
        wire.array({})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"format": 2}, "stream format"),
        ({"format": True}, "stream format"),
        ({"service": "other"}, "stream service"),
        ({"release": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}, "stream release"),
        ({"extra": 1}, "unknown object key"),
        ({"files": {}}, "file manifest mismatch"),
        ({"secrets": {}}, "secret manifest mismatch"),
    ],
)
def test_manifest_refusals(change: dict[str, wire.Json], reason: str) -> None:
    source = members()
    headers: dict[str, str] = {}
    source[0] = ("manifest.json", manifest(**change), tarfile.REGTYPE, headers)
    with pytest.raises(wire.RefusalError) as caught:
        wire.unpack(archive(source), REQUEST, {"token"}, 65536)
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (("/release/x", b"", b"0", {}), "absolute member path"),
        (("release/../x", b"", b"0", {}), "parent member path"),
        (("release/config", b"", b"0", {}), "duplicate member"),
        (("release/./config", b"", b"0", {}), "duplicate member"),
        (("outside/x", b"", b"0", {}), "member location"),
        (("secrets/other", b"", b"0", {}), "unlisted secret"),
        (("release/link", b"", b"2", {}), "member type"),
        (("release/hard", b"", b"1", {}), "member type"),
        (("release/fifo", b"", b"6", {}), "member type"),
        (("release/device", b"", b"3", {}), "member type"),
        (("release/x", b"", b"0", {"comment": "x"}), "pax header"),
    ],
)
def test_member_refusals(
    member: tuple[str, bytes, bytes, dict[str, str]], reason: str
) -> None:
    with pytest.raises(wire.RefusalError) as caught:
        wire.unpack(archive([*members(), member]), REQUEST, {"token"}, 65536)
    assert str(caught.value) == reason


def test_missing_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    data = archive(members())
    for stream, names, limit in (
        (data, {"token"}, len(data) - 1),
        (archive(members()[:2]), {"token"}, 65536),
        (archive([]), set(), 65536),
        (b"garbage", set(), 65536),
        (archive(members()[1:]), set(), 65536),
    ):
        with pytest.raises(wire.RefusalError):
            wire.unpack(stream, REQUEST, names, limit)
    monkeypatch.setattr(wire, "MEMBERS", 2)
    with pytest.raises(wire.RefusalError, match=r"^member limit$"):
        wire.unpack(data, REQUEST, {"token"}, 65536)


def test_directories_are_safe_and_ignored() -> None:
    source = members()
    source.insert(1, ("release/", b"", tarfile.DIRTYPE, {}))
    source.insert(2, ("secrets/", b"", tarfile.DIRTYPE, {}))
    assert wire.unpack(archive(source), REQUEST, {"token"}, 65536).files == {
        "release/config": b"config"
    }


def test_empty_sparse_member_refuses() -> None:
    data = archive([*members(), ("release/sparse", b"", tarfile.GNUTYPE_SPARSE, {})])
    with pytest.raises(wire.RefusalError, match=r"^sparse member$"):
        wire.unpack(data, REQUEST, {"token"}, len(data))


def test_file_size_refuses_before_body_read() -> None:
    prefix = archive(members())[:3072]
    oversized = tarfile.TarInfo("release/oversized")
    oversized.size = 65537
    data = prefix + oversized.tobuf() + b"\0" * 1024
    with pytest.raises(wire.RefusalError, match=r"^file limit$"):
        wire.unpack(data, REQUEST, {"token"}, 65536)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "expected an object"),
        ([], "expected an object"),
        ({"extra": 1}, "unknown object key"),
    ],
)
def test_object_error_messages(value: wire.Json, reason: str) -> None:
    with pytest.raises(wire.RefusalError) as caught:
        wire.mapping(value, {"allowed"})
    assert str(caught.value) == reason


def test_schema_helpers_preserve_values() -> None:
    assert wire.mapping({"a": None}, {"a", "b"}) == {"a": None}
    assert wire.mapping({"a": None, "b": [True]}, {"a", "b"}) == {
        "a": None,
        "b": [True],
    }
    assert wire.array([None, {"x": 1}, "text"]) == [None, {"x": 1}, "text"]
    assert wire.strings([]) == []
    assert wire.strings(["a", "", "b"]) == ["a", "", "b"]
    assert wire.unique([("a", 1), ("b", 2)]) == {"a": 1, "b": 2}
    with pytest.raises(wire.RefusalError, match=r"^duplicate JSON key$"):
        wire.unique([("a", 1), ("b", 2), ("a", 1)])
    with pytest.raises(wire.RefusalError, match=r"^expected array$"):
        wire.array({})
    with pytest.raises(wire.RefusalError, match=r"^expected a string list$"):
        wire.strings("a")
    with pytest.raises(wire.RefusalError, match=r"^expected strings$"):
        wire.strings(["a", 0])
    wire.require(condition=True, reason="unused")
    with pytest.raises(wire.RefusalError, match=r"^literal reason$"):
        wire.require(condition=False, reason="literal reason")
    assert (
        wire.sha256(b"abc")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("x" * 255, "x" * 255),
        ("é" * 127 + "x", "é" * 127 + "x"),
        ("a//b/./c", "a/b/c"),
        (".hidden", ".hidden"),
    ],
)
def test_relative_normalization_and_byte_boundary(path: str, expected: str) -> None:
    assert wire.relative(path) == expected


@pytest.mark.parametrize("path", ["é" * 128, "a/../b", "..", "a/.."])
def test_relative_encoded_length_and_parent_refusal(path: str) -> None:
    reason = "invalid member path" if path.startswith("é") else "parent member path"
    with pytest.raises(wire.RefusalError) as caught:
        wire.relative(path)
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    ("service", "run_id"), [("x", None), ("a" + "0" * 31, "0"), ("a-b", "1" * 20)]
)
def test_request_field_boundaries(service: str, run_id: str | None) -> None:
    value = service + " " + "0123456789abcdef" * 2 + "01234567"
    if run_id is not None:
        value += " " + run_id
    assert wire.request(value) == wire.Request(
        service, "0123456789abcdef" * 2 + "01234567", run_id
    )


def test_pack_archive_contract() -> None:
    wanted = wire.Request("metrics", "b" * 40, None)
    data = wire.pack(
        wanted,
        {"release/z": b"z", "release/a": b"a"},
        {"first": b"one", "last": b"two"},
    )
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as stream:
        entries = stream.getmembers()
        assert [entry.name for entry in entries] == [
            "manifest.json",
            "release/z",
            "release/a",
            "secrets/first",
            "secrets/last",
        ]
        assert all(entry.isfile() and entry.mode == 0o644 for entry in entries)
        assert all(entry.uid == entry.gid == entry.mtime == 0 for entry in entries)
        source = stream.extractfile(entries[0])
        assert source is not None
        assert (
            source.read()
            == json.dumps(
                {
                    "format": 1,
                    "service": "metrics",
                    "release": "b" * 40,
                    "files": {
                        "release/z": wire.sha256(b"z"),
                        "release/a": wire.sha256(b"a"),
                    },
                    "secrets": {
                        "first": wire.sha256(b"one"),
                        "last": wire.sha256(b"two"),
                    },
                },
                sort_keys=True,
            ).encode()
        )
    result = wire.unpack(data, wanted, {"first", "last"}, len(data))
    assert result.files == {"release/z": b"z", "release/a": b"a"}
    assert result.secrets == {"first": b"one", "last": b"two"}
    assert result.digest == wire.sha256(
        json.dumps(
            {"release/a": wire.sha256(b"a"), "release/z": wire.sha256(b"z")},
            sort_keys=True,
        ).encode()
    )


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("invalid", "invalid tar stream"),
        ("empty", "missing manifest"),
        ("missing_secret", "missing secret"),
        ("first_file", "manifest first"),
        ("first_directory", "manifest first"),
    ],
)
def test_archive_refusal_reasons(kind: str, reason: str) -> None:
    data = {
        "invalid": b"invalid archive",
        "empty": archive([]),
        "missing_secret": archive(members()[:2]),
        "first_file": archive(members()[1:]),
        "first_directory": archive([("manifest.json", b"", tarfile.DIRTYPE, {})]),
    }[kind]
    with pytest.raises(wire.RefusalError) as caught:
        wire.unpack(data, REQUEST, {"token"}, 65536)
    assert str(caught.value) == reason


def test_compressed_stream_is_not_the_wire_format() -> None:
    with pytest.raises(wire.RefusalError, match=r"^invalid tar stream$"):
        wire.unpack(gzip.compress(archive(members())), REQUEST, {"token"}, 65536)


def test_stream_limit_reason() -> None:
    data = archive(members())
    with pytest.raises(wire.RefusalError, match=r"^stream limit$"):
        wire.unpack(data, REQUEST, {"token"}, len(data) - 1)


def test_exact_member_count_limit_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wire, "MEMBERS", 3)
    data = archive(members())
    assert wire.unpack(data, REQUEST, {"token"}, len(data)).files == {
        "release/config": b"config"
    }


def test_both_allowed_pax_fields_are_accepted() -> None:
    source = members()
    source[1] = (
        "release/config",
        b"config",
        tarfile.REGTYPE,
        {"path": "release/config", "size": "6"},
    )
    data = archive(source)
    assert wire.unpack(data, REQUEST, {"token"}, len(data)).files == {
        "release/config": b"config"
    }


@pytest.mark.parametrize("body_kind", ["complete", "missing", "short"])
def test_member_body_boundary_and_read_contract(body_kind: str) -> None:
    body = manifest(files={}, secrets={}).ljust(1024, b" ")
    limit = len(body)
    member = tarfile.TarInfo("manifest.json")
    member.size = limit
    stream = Mock()
    stream.read.return_value = body if body_kind == "complete" else body[:-1]
    with patch.object(tarfile, "open") as opened:
        selected = opened.return_value.__enter__.return_value
        selected.offset = 0
        selected.__iter__.return_value = iter([member])
        selected.extractfile.return_value = None if body_kind == "missing" else stream
        if body_kind == "complete":
            assert wire.unpack(bytes(1024), REQUEST, set(), 1024) == wire.Release(
                {}, {}, wire.sha256(b"{}")
            )
        else:
            with pytest.raises(wire.RefusalError) as caught:
                wire.unpack(b"archive", REQUEST, set(), limit)
            assert str(caught.value) == (
                "missing member body" if body_kind == "missing" else "member size"
            )
    selected.extractfile.assert_called_once_with(member)
    if body_kind == "missing":
        stream.read.assert_not_called()
    else:
        stream.read.assert_called_once_with(limit + 1)


@pytest.mark.parametrize("size", [-1, 65537])
def test_member_size_is_bounded_before_extracting(size: int) -> None:
    member = tarfile.TarInfo("manifest.json")
    member.size = size
    with patch.object(tarfile, "open") as opened:
        selected = opened.return_value.__enter__.return_value
        selected.offset = 0
        selected.__iter__.return_value = iter([member])
        with pytest.raises(wire.RefusalError, match=r"^file limit$"):
            wire.unpack(b"archive", REQUEST, set(), 65536)
    selected.extractfile.assert_not_called()


def test_pack_explicitly_selects_pax_output() -> None:
    with patch.object(tarfile, "open", wraps=tarfile.open) as opened:
        wire.pack(REQUEST, {}, {})
    writing = opened.call_args_list[0]
    assert set(writing.kwargs) == {"fileobj", "mode", "format"}
    assert isinstance(writing.kwargs["fileobj"], io.BytesIO)
    assert writing.kwargs["mode"] == "w"
    assert writing.kwargs["format"] == tarfile.PAX_FORMAT


@pytest.mark.parametrize("trailing", [b"x", b"\0", bytes(512), b"second archive"])
def test_single_stream_rejects_any_bytes_after_end_marker(trailing: bytes) -> None:
    data = wire.pack(REQUEST, {}, {})
    with pytest.raises(wire.RefusalError, match=r"^tar end framing$"):
        wire.unpack(data + trailing, REQUEST, set(), len(data + trailing))


def test_concatenated_tar_stream_refuses() -> None:
    data = wire.pack(REQUEST, {}, {})
    with pytest.raises(wire.RefusalError, match=r"^tar end framing$"):
        wire.unpack(data + data, REQUEST, set(), len(data) * 2)


@pytest.mark.parametrize("removed", [1, 512, 1024])
def test_two_complete_zero_blocks_are_required(removed: int) -> None:
    data = wire.pack(REQUEST, {}, {})[:-removed]
    with pytest.raises(wire.RefusalError, match=r"^tar end framing$"):
        wire.unpack(data, REQUEST, set(), len(data))


def chained_pax() -> bytes:
    header = tarfile.TarInfo("pax")
    header.type = tarfile.XHDTYPE
    return header.tobuf() * 400 + wire.pack(REQUEST, {"release/config": b"x"}, {})


def test_chained_pax_headers_refuse_without_uncaught_recursion() -> None:
    data = chained_pax()
    with pytest.raises(wire.RefusalError, match=r"^invalid tar stream$"):
        wire.unpack(data, REQUEST, set(), len(data))


@pytest.mark.parametrize(
    "failure", [ValueError, IndexError, OverflowError, RecursionError]
)
def test_tar_parser_exceptions_are_controlled_refusals(
    failure: type[Exception],
) -> None:
    with (
        patch.object(tarfile, "open", side_effect=failure),
        pytest.raises(wire.RefusalError, match=r"^invalid tar stream$"),
    ):
        wire.unpack(b"", REQUEST, set(), 0)


@pytest.mark.parametrize("name", ["release", "secrets"])
def test_bare_namespace_file_cannot_be_interpreted_as_a_secret(name: str) -> None:
    source = members()
    headers: dict[str, str] = {}
    source[0] = (
        "manifest.json",
        manifest(
            secrets={"token": wire.sha256(b"fixture"), name: wire.sha256(b"bare")}
        ),
        tarfile.REGTYPE,
        headers,
    )
    source.append((name, b"bare", tarfile.REGTYPE, headers))
    with pytest.raises(wire.RefusalError, match=r"^namespace must be a directory$"):
        wire.unpack(archive(source), REQUEST, {"token", name}, 65536)
