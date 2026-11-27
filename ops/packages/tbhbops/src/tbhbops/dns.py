"""Render the DNS project's `DNS_LAN_TAILNET_ADDRS` value.

The Corefile puts the value inside a list, `client_ip() in [{$DNS_LAN_TAILNET_ADDRS}]`,
so it must be single-quoted, comma-separated strings. Each address is parsed, so
nothing else can reach the expression.
"""

import ipaddress


class AddressListError(ValueError):
    """A list that can't render: a bad element or a repeated address."""

    def __init__(self, subject: object, problem: str) -> None:
        super().__init__(f"{subject!r} {problem}")


def render_lan_tailnet_addrs(addresses: list[str]) -> str:
    """Return the addresses as `'a','b'`, or an empty string for no addresses.

    Raises AddressListError for an element that isn't an IPv4 or IPv6 address,
    one with an IPv6 zone id, an IPv4-mapped IPv6 address, and a repeated address,
    which means the caller's list is wrong.
    """
    parsed: list[str] = []
    for address in addresses:
        if not isinstance(address, str):
            raise AddressListError(address, "is not an address string")
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise AddressListError(address, "is not an IPv4 or IPv6 address") from error
        if getattr(parsed_address, "scope_id", None) is not None:
            # A zone id after % is free text, so it could close the quoted list.
            raise AddressListError(address, "has an IPv6 zone id")
        if getattr(parsed_address, "ipv4_mapped", None) is not None:
            raise AddressListError(address, "is an IPv4-mapped IPv6 address")
        parsed.append(str(parsed_address))
    if len(set(parsed)) != len(parsed):
        raise AddressListError(addresses, "has a repeated address")
    return ",".join(f"'{address}'" for address in parsed)
