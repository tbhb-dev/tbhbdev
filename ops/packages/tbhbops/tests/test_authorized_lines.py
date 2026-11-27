import socket
from typing import TYPE_CHECKING, cast

import pytest

from tbhbops import authorized_lines as al
from tbhbops.tailnet_addresses import RANGES, TailnetAddressError

if TYPE_CHECKING:
    from collections.abc import Mapping

V4, V6 = RANGES
KEY = "ssh-ed25519 AAAA agent@host"


class Resolver:
    def __init__(self, table: Mapping[str, list[str]]) -> None:
        self.table = table
        self.calls: list[str] = []

    def __call__(
        self, host: str, port: object, **kwargs: int
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del port, kwargs
        self.calls.append(host)
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 0),
            )
            for address in self.table[host]
        ]


def _table() -> dict[str, list[str]]:
    return {
        "a": [str(V4[1])],
        "b": [str(V6[2]), str(V4[2]), str(V6[2])],
        "c": [str(V4[3])],
    }


def test_lines_are_sorted_and_limited_to_addresses() -> None:
    keys = {"b": KEY + "b", "a": KEY + "a"}
    assert al.authorized_lines(keys, None, Resolver(_table())) == [
        f'from="{V4[1]}" {KEY}a',
        f'from="{V4[2]},{V6[2]}" {KEY}b',
    ]


def test_exclude_skips_the_host_without_resolving_it() -> None:
    resolver = Resolver(_table())
    keys = {"a": KEY, "b": KEY, "c": KEY}
    lines = al.authorized_lines(keys, "b", resolver)
    assert len(lines) == 2
    assert resolver.calls == ["a", "c"]


def test_an_unknown_exclude_skips_nothing() -> None:
    assert len(al.authorized_lines({"a": KEY}, "zz", Resolver(_table()))) == 1


def test_an_empty_map_gives_no_lines() -> None:
    assert al.authorized_lines({}, None, Resolver({})) == []


@pytest.mark.parametrize("host", ["", "A", "-a", "a-", "a b", 'a"', "a,b", 5, None])
def test_a_bad_host_name_is_named(host: object) -> None:
    with pytest.raises(
        al.AuthorizedLinesError, match=r"is not a plain host name$"
    ) as e:
        al.authorized_lines(cast("dict[str, str]", {host: KEY}), None, Resolver({}))
    assert str(e.value).startswith(repr(host))


@pytest.mark.parametrize(
    "key",
    [
        "",
        "ssh-dss AAAA x",
        "ssh-ed25519 !!!",
        'command="x" ssh-ed25519 AAAA x',
        'from="a.example" ssh-ed25519 AAAA x',
        KEY + "\nssh-ed25519 AAAA",
        KEY + "\r",
        " " + KEY,
        5,
        None,
    ],
)
def test_a_bad_key_names_the_host(key: object) -> None:
    with pytest.raises(
        al.AuthorizedLinesError,
        match=(
            r"^'a' has a key that is not one line of type, base64 and comment, "
            r"without options$"
        ),
    ):
        al.authorized_lines(cast("dict[str, str]", {"a": key}), None, Resolver({}))


@pytest.mark.parametrize(
    "key",
    [
        "ssh-ed25519 AAAA",
        "ecdsa-sha2-nistp256 AAAA c",
        "ssh-rsa AAAA+/= c d",
        "sk-ssh-ed25519@openssh.com AAAA",
    ],
)
def test_every_key_type_is_accepted(key: str) -> None:
    assert al.authorized_lines({"a": key}, None, Resolver(_table())) == [
        f'from="{V4[1]}" {key}'
    ]


def test_a_map_is_required() -> None:
    with pytest.raises(
        al.AuthorizedLinesError, match=r"^\[.*\] is not a map of host name to key$"
    ):
        al.authorized_lines([KEY], None, Resolver({}))  # pyrefly: ignore[bad-argument-type]


def test_a_host_outside_the_tailnet_fails_closed() -> None:
    resolver = Resolver({"a": [str(V4[0] - 1)]})
    with pytest.raises(TailnetAddressError, match=r"^a resolves outside"):
        al.authorized_lines({"a": KEY}, None, resolver)


def test_mixed_key_types_are_named_not_a_sort_error() -> None:
    keys = cast("dict[str, str]", {"a": KEY, 5: KEY})
    with pytest.raises(al.AuthorizedLinesError, match=r"^5 is not a plain host name$"):
        al.authorized_lines(keys, None, Resolver(_table()))
