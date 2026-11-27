"""Filter plugin: how DSM accounts fall short of the groups and shell required.

The logic lives in packages/tbhbops, tbhbops.dsm_accounts. The Ansible
toolchain's Python can't install that package (see ansible/pyproject.toml), so
this wrapper puts the package's source on the path.
"""

import sys
from pathlib import Path

DOCUMENTATION = """
  name: dsm_account_gaps
  short_description: List where DSM accounts miss a required group or shell
  description:
    - Reads the text of /etc/passwd as input and the text of /etc/group as an argument.
    - Returns one line per gap, and an empty list when there are none.
    - An account that doesn't exist gets a single gap.
  options:
    _input:
      description: The text of /etc/passwd.
      type: str
      required: true
    group:
      description: The text of /etc/group.
      type: str
      required: true
    accounts:
      description: The accounts to check.
      type: list
      elements: str
      required: true
    groups:
      description: The groups each account must be in.
      type: list
      elements: str
      required: true
    shell:
      description: The shell each account must have.
      type: str
      required: true
"""

EXAMPLES = """
- name: Check the accounts
  ansible.builtin.debug:
    msg: >-
      {{ passwd | dsm_account_gaps(group, ['agent', 'ops'], ['docker'], '/bin/sh') }}
"""

RETURN = """
  _value:
    description: One line per gap.
    type: list
    elements: str
"""

_SRC = Path(__file__).resolve().parents[3] / "packages" / "tbhbops" / "src"


def _gaps(
    passwd: str, group: str, accounts: list[str], groups: list[str], shell: str
) -> list[str]:
    sys.path.insert(0, str(_SRC))
    from tbhbops.dsm_accounts import account_gaps  # noqa: PLC0415

    return account_gaps(passwd, group, accounts, groups, shell)


class FilterModule:
    def filters(self) -> dict[str, object]:
        return {"dsm_account_gaps": _gaps}
