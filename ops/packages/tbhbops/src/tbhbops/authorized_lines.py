"""Build authorized_keys lines that limit each key to its own host.

Ansible uses this at apply time. Git holds one public key per host, without
options. Each line gets a `from=` option naming the host's tailnet addresses,
because sshd matches a hostname in `from=` only with `UseDNS yes`, which is off
by default, so a hostname would lock the key out.
"""

import re
from collections.abc import Mapping

from tbhbops.tailnet_addresses import Resolver, tailnet_addresses

# The windows_accounts role's key pattern: one line, no options.
KEY_PATTERN = re.compile(
    r"^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa|sk-ssh-ed25519@openssh\.com)"
    r" [A-Za-z0-9+/=]+( [^\r\n]*)?$"
)
HOST_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class AuthorizedLinesError(ValueError):
    """A host name or key line that can't become a restricted key line."""

    def __init__(self, host: object, problem: str) -> None:
        super().__init__(f"{host!r} {problem}")


def _line(host: object, key: object, resolver: Resolver) -> str:
    if not isinstance(host, str) or not HOST_PATTERN.fullmatch(host):
        raise AuthorizedLinesError(host, "is not a plain host name")
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise AuthorizedLinesError(
            host,
            "has a key that is not one line of type, base64 and comment, "
            "without options",
        )
    addresses = tailnet_addresses([host], resolver)
    return f'from="{",".join(addresses)}" {key}'


def authorized_lines(
    keys: Mapping[str, str],
    exclude: str | None,
    resolver: Resolver,
) -> list[str]:
    """Return one `from="<addresses>" <key>` line per host, sorted by host.

    Skips `exclude`. Raises AuthorizedLinesError, naming the host, on a bad host
    name or key, and TailnetAddressError on a host that doesn't resolve into the
    tailnet. `resolver` has the signature of `socket.getaddrinfo`, which the Ansible
    plugin passes.
    """
    if not isinstance(keys, Mapping):
        raise AuthorizedLinesError(keys, "is not a map of host name to key")
    # Sorting by str() keeps a bad name from failing the sort before it is named.
    return [
        _line(host, keys[host], resolver)
        for host in sorted(keys, key=str)
        if exclude is None or host != exclude
    ]
