import pytest

from tbhbops.dns import AddressListError, render_lan_tailnet_addrs


def test_empty_list_renders_empty_string() -> None:
    assert render_lan_tailnet_addrs([]) == ""


def test_one_address_is_quoted() -> None:
    assert render_lan_tailnet_addrs(["192.0.2.1"]) == "'192.0.2.1'"


def test_addresses_are_quoted_and_comma_separated_in_order() -> None:
    assert (
        render_lan_tailnet_addrs(["192.0.2.2", "2001:db8::2", "192.0.2.1"])
        == "'192.0.2.2','2001:db8::2','192.0.2.1'"
    )


def test_ipv6_is_written_in_canonical_form() -> None:
    assert (
        render_lan_tailnet_addrs(["2001:DB8::2"]) == "'2001:db8::2'"
    )


@pytest.mark.parametrize(
    "bad",
    ["", " ", "example.com", "192.0.2.1/32", "100.64.0.256", "'x'", " 192.0.2.1"],
)
def test_bad_element_is_rejected(bad: str) -> None:
    with pytest.raises(AddressListError) as raised:
        render_lan_tailnet_addrs(["192.0.2.1", bad])
    assert str(raised.value) == f"{bad!r} is not an IPv4 or IPv6 address"
    assert isinstance(raised.value.__cause__, ValueError)


def test_non_string_element_is_rejected() -> None:
    with pytest.raises(AddressListError) as raised:
        render_lan_tailnet_addrs([16777216])  # type: ignore[list-item]
    assert str(raised.value) == "16777216 is not an address string"


def test_duplicate_is_rejected() -> None:
    with pytest.raises(AddressListError) as raised:
        render_lan_tailnet_addrs(["192.0.2.1", "192.0.2.1"])
    assert str(raised.value) == "['192.0.2.1', '192.0.2.1'] has a repeated address"


def test_duplicate_in_different_spelling_is_rejected() -> None:
    with pytest.raises(AddressListError) as raised:
        render_lan_tailnet_addrs(["2001:db8::2", "2001:DB8::2"])
    assert (
        str(raised.value)
        == "['2001:db8::2', '2001:DB8::2'] has a repeated address"
    )


@pytest.mark.parametrize(
    "scoped",
    [
        "fe80::1%eth0",
        "fe80::1%a'] || true || client_ip() in ['a",
        "2001:db8::2%1",
    ],
)
def test_zone_id_is_rejected(scoped: str) -> None:
    with pytest.raises(AddressListError) as raised:
        render_lan_tailnet_addrs(["192.0.2.1", scoped])
    assert str(raised.value) == f"{scoped!r} has an IPv6 zone id"


@pytest.mark.parametrize("mapped", ["::ffff:192.0.2.1", "::FFFF:C000:201"])
def test_ipv4_mapped_address_is_rejected(mapped: str) -> None:
    with pytest.raises(AddressListError) as raised:
        render_lan_tailnet_addrs([mapped])
    assert str(raised.value) == f"{mapped!r} is an IPv4-mapped IPv6 address"
