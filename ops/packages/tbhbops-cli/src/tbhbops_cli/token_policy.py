"""Inventory selection, names, and vault access for Connect tokens."""

from dataclasses import dataclass
from datetime import UTC
lazy from collections.abc import Collection, Sequence
lazy from datetime import datetime

from tbhbops_cli.common import CliError

VAULT_ALL = "host-0-shared"
# Holds the Tailscale OAuth client that mints runner-VM auth keys. Only the
# ops-session token reaches it.
VAULT_OAUTH = "overlay-oauth"
# The CI runner VMs, named after their lab node plus this suffix. Each one's
# token reads only VAULT_RUNNERS and is stored in the lab node's host vault.
CI = "ci"
CI_SUFFIX = "-ci"
RUNNER_PREFIX = "runner-vm"
RUNNER_ITEM = "runner-access-token"
VAULT_RUNNERS = "runner-fixture"
# The operator's Macs, which run the Ansible controller lookups. Their token
# reads every agent host's vault and writes host-0-shared.
CONTROLLERS = "operator_macs"
AGENTS = "agent_hosts"
# No agent-host token ever reaches a host-storage-* vault; only an ops-controller
# token does.
FORBIDDEN = "host-storage-"
OPS_PREFIX = "ops-controller"
SESSION_PREFIX = "ops-session"
# The hosts whose vaults host-storage-<host> hold the ops DSM passwords. The
# inventory has no group for them yet.
NAS = ("nas-a", "nas-b")
READ = "r"
# `op connect token create --vault` takes one suffix at most: vault,r or
# vault,w. With none, the token gets the server's permissions on the vault, which
# is read-write. vault,r,w is not valid: op reads it as the vault "vault,r".
READ_WRITE = ""
STAMP = "%Y%m%dT%H%M%SZ"
# Connect rejects a long token name with a 400 "structure of request was
# invalid". A 61-character name worked and a 150-character one failed, so names
# stay at most 64 characters, the length that's known to work, plus a few.
NAME_LIMIT = 64


@dataclass(frozen=True)
class Grant:
    """A token's access to one vault."""

    vault: str
    permission: str


def vault_option(grant: Grant) -> str:
    """The `--vault` argument of GRANT, with no suffix for read-write."""
    suffix = f",{grant.permission}" if grant.permission else ""
    return f"--vault {grant.vault}{suffix}"


def vault_of(host: str) -> str:
    """The name of HOST's own vault."""
    return f"host-{host}"


def token_name(prefix: str, host: str, when: str) -> str:
    """The name of HOST's PREFIX token made at WHEN, within NAME_LIMIT."""
    name = f"{prefix}:{host}:{when}"
    if len(name) > NAME_LIMIT:
        message = (
            f"the token name {name} has {len(name)} characters,"
            f" over the {NAME_LIMIT} Connect accepts"
        )
        raise CliError(message)
    return name


def stamp(now: datetime) -> str:
    """NOW as the UTC stamp in a token's name."""
    return now.astimezone(UTC).strftime(STAMP)


def select(agents: Sequence[str], wanted: Sequence[str]) -> tuple[str, ...]:
    """The hosts to make tokens for: WANTED, or every agent host if none."""
    unknown = sorted(set(wanted) - set(agents))
    if unknown:
        message = f"not an agent host in the inventory: {', '.join(unknown)}"
        raise CliError(message)
    return tuple(sorted(set(wanted))) if wanted else tuple(agents)


def node_of(vm: str) -> str:
    """The lab node that hosts the runner VM VM: its name without CI_SUFFIX."""
    return vm.removesuffix(CI_SUFFIX)


def select_runners(
    ci: Sequence[str], agents: Collection[str], wanted: Sequence[str]
) -> tuple[str, ...]:
    """The runner VMs to make tokens for, WANTED in name order.

    Each must be in the ci group CI, and its lab node an agent host, because
    the token is stored in the node's vault.
    """
    unknown = sorted(set(wanted) - set(ci))
    if unknown:
        message = f"not a CI runner VM in the inventory: {', '.join(unknown)}"
        raise CliError(message)
    for vm in sorted(set(wanted)):
        node = node_of(vm)
        if node == vm or node not in agents:
            message = (
                f"the lab node of {vm} is not an agent host in the inventory:"
                f" its token has no vault to go in"
            )
            raise CliError(message)
    return tuple(sorted(set(wanted)))


def runner_grants() -> tuple[Grant, ...]:
    """What a runner VM's token reaches: VAULT_RUNNERS, read-only, and no more."""
    return (Grant(VAULT_RUNNERS, READ),)


def grants(
    host: str, agents: Sequence[str], controllers: Collection[str]
) -> tuple[Grant, ...]:
    """What HOST's token reaches, never a host-storage-* vault.

    A controller writes host-0-shared and reads every agent host's vault. Any
    other host reads its own vault and host-0-shared.
    """
    if host in controllers:
        found = (
            Grant(VAULT_ALL, READ_WRITE),
            *(Grant(vault_of(a), READ) for a in agents),
        )
    else:
        found = (Grant(vault_of(host), READ), Grant(VAULT_ALL, READ))
    for grant in found:
        if grant.vault.startswith(FORBIDDEN):
            message = (
                f"refusing to grant {grant.vault}: no token reaches host-storage-*"
            )
            raise CliError(message)
    return found


def ops_grants() -> tuple[Grant, ...]:
    """What an ops-controller token reaches: both host-storage-* vaults, read-only."""
    return tuple(Grant(f"{FORBIDDEN}{n}", READ) for n in NAS)


def _require_controller(host: str, controllers: Collection[str]) -> None:
    if host not in controllers:
        message = f"not an operator Mac in the inventory: {host}"
        raise CliError(message)


def ops_controller(host: str, controllers: Collection[str]) -> tuple[Grant, ...]:
    """The ops-controller grants of HOST, which must be an operator Mac."""
    _require_controller(host, controllers)
    return ops_grants()


def session_grants(
    host: str, agents: Sequence[str], controllers: Collection[str]
) -> tuple[Grant, ...]:
    """The ops-session grants of HOST, which must be an operator Mac.

    They are an operator Mac's own grants plus read on VAULT_OAUTH, so they
    never reach a host-storage-* vault.
    """
    _require_controller(host, controllers)
    return (*grants(host, agents, controllers), Grant(VAULT_OAUTH, READ))


def select_ops(controllers: Collection[str], hosts: Collection[str]) -> tuple[str, ...]:
    """The operator Macs among HOSTS, which each get an ops-controller token."""
    return tuple(sorted(set(controllers) & set(hosts)))


def reach(found: Collection[str], expected: Collection[str]) -> list[tuple[str, str]]:
    """Each vault in FOUND or EXPECTED as (status, name), sorted by name.

    The status is ok, missing (expected, not found) or extra (found, not
    expected).
    """
    names = sorted(set(found) | set(expected))
    return [
        (
            "ok"
            if n in found and n in expected
            else "missing"
            if n in expected
            else "extra",
            n,
        )
        for n in names
    ]
