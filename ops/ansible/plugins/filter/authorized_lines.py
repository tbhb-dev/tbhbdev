"""Filter plugin: authorized_keys lines limited to each host's tailnet addresses.

The logic lives in packages/tbhbops, tbhbops.authorized_lines. The Ansible
toolchain's Python can't install that package (see ansible/pyproject.toml), so
this wrapper puts the package's source on the path.
"""

import socket
import sys
from pathlib import Path

from ansible.errors import AnsibleFilterError

DOCUMENTATION = """
  name: authorized_lines
  short_description: Limit each host's public key to that host's tailnet addresses
  description:
    - Takes a map of host name to one public key line without options.
    - Returns one C(from="addresses" key) line per host, sorted by host name.
    - Resolves each host name with the controller's resolver.
    - Fails, naming the host, on a bad host name or key line.
    - Fails the same way on a name that doesn't resolve into the tailnet.
  options:
    _input:
      description: Host name to public key line.
      type: dict
      required: true
    exclude:
      description: A host name to leave out, such as the host being configured.
      type: str
      default: null
"""

EXAMPLES = """
- name: Authorize every other host's key
  ansible.builtin.debug:
    msg: "{{ ssh_host_keys_agent | authorized_lines(inventory_hostname) }}"
"""

RETURN = """
  _value:
    description: The restricted key lines.
    type: list
    elements: str
"""

_SRC = Path(__file__).resolve().parents[3] / "packages" / "tbhbops" / "src"


def _lines(keys: object, exclude: str | None = None) -> list[str]:
    sys.path.insert(0, str(_SRC))
    from tbhbops.authorized_lines import (  # noqa: PLC0415
        AuthorizedLinesError,
        authorized_lines,
    )
    from tbhbops.tailnet_addresses import TailnetAddressError  # noqa: PLC0415

    try:
        return authorized_lines(keys, exclude, socket.getaddrinfo)  # pyrefly: ignore[bad-argument-type]
    except (AuthorizedLinesError, TailnetAddressError) as error:
        raise AnsibleFilterError(str(error)) from error


class FilterModule:
    def filters(self) -> dict[str, object]:
        return {"authorized_lines": _lines}
