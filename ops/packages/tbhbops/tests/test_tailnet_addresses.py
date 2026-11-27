import ipaddress
import re
import socket
from itertools import permutations
from typing import TYPE_CHECKING, cast

import pytest

from tbhbops import tailnet_addresses as ta

if TYPE_CHECKING:
    from collections.abc import Mapping

V4, V6 = ta.RANGES
type Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _info(address: str) -> tuple[int, int, int, str, tuple[str, int]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (address, 0))


class Resolver:
    def __init__(self, table: Mapping[str, list[str] | Exception]) -> None:
        self.table = table
        self.calls: list[tuple[str, object, dict[str, int]]] = []

    def __call__(
        self, host: str, port: object, **kwargs: int
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        self.calls.append((host, port, kwargs))
        result = self.table[host]
        if isinstance(result, Exception):
            raise result
        return [_info(address) for address in result]


def test_returns_both_families_without_duplicates() -> None:
    one, two = str(V4[1]), str(V6[1])
    resolver = Resolver({"a": [one, two, one], "b": [one]})
    assert ta.tailnet_addresses(["a", "b"], resolver) == [one, two]
    assert resolver.calls[0] == ("a", None, {"type": socket.SOCK_STREAM})


def test_resolver_order_does_not_change_numeric_address_order() -> None:
    expected = [str(V4[9]), str(V4[10]), str(V6[9]), str(V6[16])]
    for shuffled in permutations(expected):
        resolver = Resolver({"a": [*shuffled, shuffled[0]], "b": [expected[0]]})
        assert ta.tailnet_addresses(["a", "b"], resolver) == expected


@pytest.mark.parametrize("address", [V4[0], V4[-1], V6[0], V6[-1]])
def test_range_edges_are_accepted(address: Address) -> None:
    assert ta.tailnet_addresses(["a"], Resolver({"a": [str(address)]})) == [
        str(address)
    ]


@pytest.mark.parametrize(
    "address",
    [
        V4[0] - 1,
        V4[-1] + 1,
        V6[0] - 1,
        V6[-1] + 1,
        ipaddress.ip_address(0),
        ipaddress.ip_address(2**128 - 1),
    ],
)
def test_outside_the_ranges_names_the_host(address: Address) -> None:
    resolver = Resolver({"nas": [str(address)]})
    with pytest.raises(
        ta.TailnetAddressError, match=r"^nas resolves outside the tailnet ranges$"
    ):
        ta.tailnet_addresses(["nas"], resolver)


def test_one_bad_address_fails_the_whole_host() -> None:
    resolver = Resolver({"nas": [str(V4[1]), str(V4[0] - 1)]})
    with pytest.raises(ta.TailnetAddressError, match=r"^nas "):
        ta.tailnet_addresses(["nas"], resolver)


def test_a_failed_lookup_names_the_host() -> None:
    resolver = Resolver({"nas": socket.gaierror("no such name")})
    with pytest.raises(
        ta.TailnetAddressError, match=r"^nas does not resolve: no such name$"
    ) as raised:
        ta.tailnet_addresses(["nas"], resolver)
    assert isinstance(raised.value.__cause__, socket.gaierror)


def test_a_non_address_result_names_the_host() -> None:
    resolver = Resolver({"nas": ["not-an-address"]})
    with pytest.raises(
        ta.TailnetAddressError,
        match=r"^nas resolves to 'not-an-address', not an address$",
    ):
        ta.tailnet_addresses(["nas"], resolver)


def test_no_result_names_the_host() -> None:
    with pytest.raises(ta.TailnetAddressError, match=r"^nas resolves to no address$"):
        ta.tailnet_addresses(["nas"], Resolver({"nas": []}))


@pytest.mark.parametrize("bad", ["", None, 5])
def test_a_bad_hostname_is_rejected(bad: object) -> None:
    with pytest.raises(
        ta.TailnetAddressError, match=f"^{re.escape(repr(bad))} is not a hostname$"
    ):
        ta.tailnet_addresses([cast("str", bad)], Resolver({}))


def test_empty_input_is_an_error() -> None:
    with pytest.raises(
        ta.TailnetAddressError, match=r"^\[\] is an empty list of hostnames$"
    ):
        ta.tailnet_addresses([], Resolver({}))


def test_a_bare_string_is_an_error() -> None:
    resolver = Resolver({})
    with pytest.raises(
        ta.TailnetAddressError,
        match=r"^'mac-a' is a string, not a list of hostnames$",
    ):
        ta.tailnet_addresses("mac-a", resolver)  # pyrefly: ignore[string-as-iterable]
    assert resolver.calls == []
