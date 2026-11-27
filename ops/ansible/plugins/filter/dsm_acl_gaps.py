"""Filter plugin: which accounts lack a first-place deny entry in a share's ACL.

The logic lives in packages/tbhbops, tbhbops.dsm_accounts. The Ansible
toolchain's Python can't install that package (see ansible/pyproject.toml), so
this wrapper puts the package's source on the path.
"""

import sys
from pathlib import Path

DOCUMENTATION = """
  name: dsm_acl_gaps
  short_description: List the accounts a share's ACL doesn't deny first
  description:
    - Reads the output of C(synoacltool -get) as input.
    - Returns one line per account that has no user entry denying every permission.
    - Also returns a line for an account whose deny entry sits after an allow entry.
    - Returns an empty list when every account is denied first.
  options:
    _input:
      description: The output of C(synoacltool -get) for a share's path.
      type: str
      required: true
    accounts:
      description: The accounts that need a deny entry.
      type: list
      elements: str
      required: true
"""

EXAMPLES = """
- name: Check a share's ACL
  ansible.builtin.debug:
    msg: "{{ acl.stdout | dsm_acl_gaps(['agent', 'ops']) }}"
"""

RETURN = """
  _value:
    description: One line per account.
    type: list
    elements: str
"""

_SRC = Path(__file__).resolve().parents[3] / "packages" / "tbhbops" / "src"


def _gaps(acl: str, accounts: list[str]) -> list[str]:
    sys.path.insert(0, str(_SRC))
    from tbhbops.dsm_accounts import acl_gaps  # noqa: PLC0415

    return acl_gaps(acl, accounts)


class FilterModule:
    def filters(self) -> dict[str, object]:
        return {"dsm_acl_gaps": _gaps}
