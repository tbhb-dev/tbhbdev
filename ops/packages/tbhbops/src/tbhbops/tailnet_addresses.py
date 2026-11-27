"""Resolve tailnet hostnames to their tailnet addresses.

Ansible uses this at apply time to write sshd `from=` options, which match an
address and not a hostname unless sshd runs with `UseDNS yes`. Git holds the
hostnames only. The lookup fails closed: it never returns a hostname, a
wildcard, or an empty list, because a `from=` option built from any of those
is wider than the host it names.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable

# Tailscale's address ranges: the CGNAT block for IPv4 and its ULA prefix for
# IPv6. An address outside both isn't a tailnet address.
RANGES = (
    ipaddress.IPv4Network((bytes((100, 64, 0, 0)), 10)),
    ipaddress.IPv6Network((int.from_bytes(bytes.fromhex("fd7a 115c a1e0")) << 80, 48)),
)

type Resolver = Callable[
    ..., Iterable[tuple[object, object, object, object, tuple[object, ...]]]
]


class TailnetAddressError(ValueError):
    """A hostname that doesn't resolve to tailnet addresses only."""

    def __init__(self, subject: str, problem: str) -> None:
        super().__init__(f"{subject} {problem}")


def _lookup(hostname: str, resolver: Resolver) -> list[str]:
    if not isinstance(hostname, str) or not hostname:
        raise TailnetAddressError(repr(hostname), "is not a hostname")
    try:
        found = list(resolver(hostname, None, type=socket.SOCK_STREAM))
    except OSError as error:
        raise TailnetAddressError(hostname, f"does not resolve: {error}") from error
    if not found:
        raise TailnetAddressError(hostname, "resolves to no address")
    addresses: list[str] = []
    for info in found:
        text = str(info[4][0])
        try:
            address = ipaddress.ip_address(text)
        except ValueError as error:
            raise TailnetAddressError(
                hostname, f"resolves to {text!r}, not an address"
            ) from error
        if not any(
            address in network
            for network in RANGES
            if address.version == network.version
        ):
            raise TailnetAddressError(hostname, "resolves outside the tailnet ranges")
        addresses.append(str(address))
    return addresses


def tailnet_addresses(hostnames: Iterable[str], resolver: Resolver) -> list[str]:
    """Return unique tailnet addresses, IPv4 before IPv6, sorted numerically.

    Raises TailnetAddressError, naming the hostname, when a hostname isn't a
    string, doesn't resolve, or resolves to an address outside the tailnet.
    It also raises when `hostnames` is empty. `resolver` has the signature of
    `socket.getaddrinfo`, which the Ansible plugin passes.
    """
    if isinstance(hostnames, str):
        # A bare string would iterate as characters and resolve each one.
        raise TailnetAddressError(
            repr(hostnames), "is a string, not a list of hostnames"
        )
    addresses = {
        ipaddress.ip_address(address)
        for hostname in hostnames
        for address in _lookup(hostname, resolver)
    }
    if not addresses:
        raise TailnetAddressError(repr([]), "is an empty list of hostnames")
    return [
        str(address)
        for address in sorted(
            addresses, key=lambda address: (address.version, int(address))
        )
    ]
