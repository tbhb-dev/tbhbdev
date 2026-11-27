"""The hosts and the operators in the Ansible inventory, ops/ansible/inventory/ of the companion.

The inventory is the one list of hosts. A host's tailnet name is its
`ansible_host` in host_vars when it has one, as edge-1 does, and otherwise
its inventory name. Both must be a short name: an address, or a name under a
domain, would put what the generated config exists to keep out into the
inventory's reader, so it is refused, by the inventory name.
"""

import os
import re
from collections.abc import Mapping
lazy from pathlib import Path

import yaml

from tbhbops_cli.common import CliError

SHORT_NAME = r"[A-Za-z][A-Za-z0-9-]*"
OPERATOR_VAR = "agent_account_operator"
RETIRED = "retired-hosts.yml"
# On a case-insensitive filesystem a name in capitals finds the same directory,
# so no test tells these from their mutants there.
HOST_VARS = "host_vars"  # pragma: no mutate  # HOST_VARS, on macOS
GROUP_VARS = "group_vars"  # pragma: no mutate  # GROUP_VARS, on macOS


def find(start: Path, env: Mapping[str, str] | None = None) -> Path:
    """Find deployment inventory in the companion to the monorepo."""
    environ = os.environ if env is None else env
    start = start.resolve()
    anchor = start.parent if start.is_file() else start
    root = next(
        (directory for directory in (anchor, *anchor.parents)
         if (directory / "ops" / "ansible" / "ansible.cfg").is_file()),
        anchor,
    )
    companion = Path(environ.get("TBHB_PRIVATE_ROOT", "../tbhbdev-private")).expanduser()
    directory = (root / companion / "ops" / "ansible" / "inventory").resolve()
    if (directory / "hosts.yml").is_file():
        return directory
    message = (
        f"no ansible/inventory/hosts.yml in the companion at {directory}; "
        "set TBHB_PRIVATE_ROOT to the companion checkout"
    )
    raise CliError(message)


def at(directory: Path) -> Path:
    """DIRECTORY, which must be an inventory directory, as `find` returns one."""
    if not (
        directory / "hosts.yml"  # pragma: no mutate  # HOSTS.YML, on macOS
    ).is_file():
        message = f"no hosts.yml in {directory}"
        raise CliError(message)
    return directory


def _load(path: Path) -> object:
    try:
        loaded: object = yaml.safe_load(path.read_bytes())
    except OSError, yaml.YAMLError:
        # The parser's message quotes the offending line, so it stays out.
        message = f"cannot read {path.name} as YAML"
        raise CliError(message) from None
    return loaded


def _object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _group(group: object) -> set[str]:
    """The names of the hosts in GROUP and in its children."""
    found = set(_object(_object(group).get("hosts")))
    for child in _object(_object(group).get("children")).values():
        found |= _group(child)
    return found


type _Groups = dict[str, tuple[set[str], set[str]]]


def _definitions(group: object, found: _Groups) -> None:
    """Add each group below GROUP to FOUND: its hosts and its child names.

    A child may be named with no body, as agent_hosts names lab, and defined
    elsewhere. Every definition of a name adds to the same entry.
    """
    for name, child in _object(_object(group).get("children")).items():
        hosts, kids = found.setdefault(name, (set(), set()))
        hosts |= set(_object(_object(child).get("hosts")))
        kids |= set(_object(_object(child).get("children")))
        _definitions(child, found)


def _flatten(found: _Groups, name: str, seen: set[str]) -> set[str]:
    """The hosts in the group NAME and in its children, which SEEN stops cycling."""
    if name in seen:
        return set()
    seen.add(name)
    hosts, kids = found[name]
    members = set(hosts)
    for kid in kids:
        members |= _flatten(found, kid, seen)
    return members


def _vars(directory: Path, kind: str, name: str) -> dict[str, object]:
    path = directory / kind / f"{name}.yml"
    return _object(_load(path)) if path.is_file() else {}


def hosts(directory: Path) -> tuple[str, ...]:
    """Every host's tailnet name, lowercase and sorted."""
    root = _load(directory / "hosts.yml")
    names = _group(_object(root).get("all"))
    found: set[str] = set()
    for name in names:
        alias = _vars(directory, HOST_VARS, name).get("ansible_host", name)
        if not isinstance(alias, str) or not re.fullmatch(SHORT_NAME, alias):
            message = f"host {name} has no short tailnet name in the inventory"
            raise CliError(message)
        found.add(alias.lower())
    if not found:
        message = f"{directory / 'hosts.yml'} lists no hosts"
        raise CliError(message)
    return tuple(sorted(found))


def retired_hosts(directory: Path) -> tuple[str, ...]:
    """The tailnet names of the retired hosts, lowercase and sorted.

    A host that left hosts.yml keeps its exact rule through this list.
    """
    path = directory / RETIRED
    if not path.is_file():
        message = f"{path} is missing; it lists the retired hosts, maybe none"
        raise CliError(message)
    listed = _object(_load(path)).get("retired_hosts")
    names: list[object] = listed if isinstance(listed, list) else []
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(SHORT_NAME, name):
            message = f"{path.name} has a retired host with no short tailnet name"
            raise CliError(message)
    return tuple(sorted({str(name).lower() for name in names}))


def group(directory: Path, name: str) -> tuple[str, ...]:
    """The inventory names of the hosts in the group NAME, sorted.

    These are inventory names, which a host's vault is named after, and not
    the tailnet names that `hosts` returns.
    """
    found: _Groups = {}
    _definitions(_object(_load(directory / "hosts.yml")).get("all"), found)
    members = _flatten(found, name, set()) if name in found else set()
    if not members:
        message = f"{directory / 'hosts.yml'} has no group {name} with hosts"
        raise CliError(message)
    for member in members:
        if not re.fullmatch(SHORT_NAME, member):
            message = f"host {member} in group {name} isn't a short name"
            raise CliError(message)
    return tuple(sorted(members))


def operators(directory: Path) -> tuple[str, ...]:
    """The operator names in group_vars and host_vars, sorted."""
    found: set[str] = set()
    for kind in (GROUP_VARS, HOST_VARS):
        for path in sorted((directory / kind).glob("*.yml")):
            value = _vars(directory, kind, path.stem).get(OPERATOR_VAR)
            if isinstance(value, str) and value:
                found.add(value)
    if not found:
        message = f"the inventory sets no {OPERATOR_VAR}"
        raise CliError(message)
    return tuple(sorted(found))
