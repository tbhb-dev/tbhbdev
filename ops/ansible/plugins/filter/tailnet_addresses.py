"""Filter plugin: resolve tailnet hostnames to tailnet addresses.

The logic lives in packages/tbhbops, tbhbops.tailnet_addresses. The Ansible
toolchain's Python can't install that package (see ansible/pyproject.toml), so
this wrapper puts the package's source on the path.
"""

import socket
import sys
from pathlib import Path

from ansible.errors import AnsibleFilterError

DOCUMENTATION = """
  name: tailnet_addresses
  short_description: Resolve tailnet host names to their tailnet addresses
  description:
    - Resolves each host name with the controller's resolver.
    - Returns unique addresses, IPv4 before IPv6, each sorted numerically.
    - Fails, naming the host, on a name that doesn't resolve.
    - Fails the same way on a name that resolves outside the tailnet ranges.
    - Never returns the host name, a wildcard, or an empty list.
  options:
    _input:
      description: Tailnet host names.
      type: list
      elements: str
      required: true
"""

EXAMPLES = """
- name: Build the from= option for a key
  ansible.builtin.debug:
    msg: from="{{ ['mac-a', 'mac-b'] | tailnet_addresses | join(',') }}"
"""

RETURN = """
  _value:
    description: Unique tailnet addresses, IPv4 before IPv6, each sorted numerically.
    type: list
    elements: str
"""

_SRC = Path(__file__).resolve().parents[3] / "packages" / "tbhbops" / "src"


def _resolve(hostnames: list[str]) -> list[str]:
    sys.path.insert(0, str(_SRC))
    from tbhbops.tailnet_addresses import (  # noqa: PLC0415
        TailnetAddressError,
        tailnet_addresses,
    )

    try:
        return tailnet_addresses(hostnames, socket.getaddrinfo)
    except TailnetAddressError as error:
        raise AnsibleFilterError(str(error)) from error


class FilterModule:
    def filters(self) -> dict[str, object]:
        return {"tailnet_addresses": _resolve}
