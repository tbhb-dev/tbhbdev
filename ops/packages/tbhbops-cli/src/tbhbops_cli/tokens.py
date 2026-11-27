"""The Connect tokens of the agent hosts, and the script that creates them.

This holds each token's grants, the script, and the reach check.

Each agent host holds one token with no expiry, in the item `connect-token`
of its own vault `host-<host>`, which the Ansible `connect_token` role reads
from the vault and item configured for that host.
Each operator Mac also gets an ops-controller token, read-only on the
host-storage-* vaults, which goes into the `ops` user's keychain on that Mac and
into no vault. On request, the operator's own agent session also gets an
ops-session token, which reads like an operator Mac's token, also reads the
vault overlay-oauth (the Tailscale OAuth client that mints runner-VM auth
keys), and goes into the keychain of the user who runs the script. No other
token reaches overlay-oauth. Each CI runner VM, on request, gets a token that
reads only the vault runner-fixture, which holds the runner App's key. It sits
in the item `runner-access-token` of its lab node's vault `host-<node>`, which
the Ansible `actions_runner_pool` role reads at
from the vault and item configured for the node. The command
prints the script and never runs it.
"""

import shlex
lazy from collections.abc import Collection, Iterable, Sequence

from tbhbops_cli.token_keychain import OPS_USER, keychain_block
from tbhbops_cli.token_policy import (
    OPS_PREFIX,
    RUNNER_ITEM,
    RUNNER_PREFIX,
    SESSION_PREFIX,
    VAULT_ALL,
    VAULT_RUNNERS,
    Grant,
    grants,
    node_of,
    ops_grants,
    runner_grants,
    session_grants,
    token_name,
    vault_of,
)
from tbhbops_cli.token_vaults import FIELD, ITEM, stored_block, vault_block

PREFIX = "agent-host"


def _live(text: str) -> str:
    return text


def _echoed(text: str) -> str:
    return f"echo {shlex.quote(text)}"


def _header(  # the header's inputs, each one a fact
    server: str,
    *,
    with_ops: bool,
    session: str | None,
    runners: bool,
    runners_only: bool,
    dry_run: bool,
) -> list[str]:
    """The script's first lines: set -eu and the comments that say what it does."""
    lines = ["set -eu", ""]
    if dry_run:
        tools = {
            (False, False): "op command",
            (True, False): "op and ssh command",
            (False, True): "op and security command",
            (True, True): "op, ssh and security command",
        }[with_ops, session is not None]
        lines.append(f"# Dry run: each {tools} below is echoed, not run.")
    else:
        needs = {
            (False, False): "op and jq",
            (True, False): "op, jq and ssh",
            (False, True): "op, jq and security",
            (True, True): "op, jq, ssh and security",
        }[with_ops, session is not None]
        lines += [
            "# @tbhb runs this script once, in their own terminal, and 1Password asks",
            "# them to approve once.",
            f"# An agent only prints it. It needs {needs}.",
        ]
    if runners_only:
        return [
            *lines,
            "# It first creates each vault it needs that the account lacks.",
            (
                f"# It grants the Connect server {server} the vault {VAULT_RUNNERS}"
                " and each lab node's vault,"
            ),
            "# then creates one token with no expiry for each CI runner VM.",
            f"# The token reads only the vault {VAULT_RUNNERS}, read-only. It is kept",
            f"# in its lab node's vault as the item {RUNNER_ITEM}, field {FIELD}.",
            "# A VM whose node vault already holds it is skipped, so the script is",
            "# safe to run again.",
            "",
        ]
    lines += [
        "# It first creates each vault it needs that the account lacks.",
        (
            f"# It grants the Connect server {server} its vaults, then creates one"
            " token with no expiry for each host"
        ),
        f"# and stores it in the host's vault as the item {ITEM}, field {FIELD}.",
        f"# A host whose vault already holds {ITEM} is skipped, so the script is safe",
        "# to run again.",
    ]
    if with_ops:
        lines += [
            f"# Each operator Mac also gets an {OPS_PREFIX} token, read-only on the",
            f"# vaults of the ops accounts. It goes into the {OPS_USER} user's",
            "# keychain over ssh as that user, and into no vault. A Mac where that",
            "# user already holds it is skipped.",
        ]
    if runners:
        lines += [
            f"# Each CI runner VM also gets a {RUNNER_PREFIX} token, which reads only",
            f"# the vault {VAULT_RUNNERS}, read-only. It is stored in its lab node's",
            f"# vault as the item {RUNNER_ITEM}, field {FIELD}, and skipped if the",
            "# vault already holds it.",
        ]
    if session:
        lines += [
            f"# {session} also gets an {SESSION_PREFIX} token, which reads every",
            "# agent host's vault, writes host-0-shared and reads the vault overlay-oauth,",
            "# which holds the Tailscale OAuth client that mints runner-VM auth keys.",
            "# It goes into the keychain of the user who runs the script, with no",
            "# ssh, and into no vault. It is skipped if that keychain already holds",
            "# it.",
        ]
    lines.append("")
    return lines


def _granted(
    plans: Iterable[Sequence[Grant]], *, with_ops: bool, stores: Iterable[str] = ()
) -> list[str]:
    """The vaults, besides host-0-shared, the server must grant for PLANS, sorted.

    STORES are vaults the script only writes items to, which the server
    still grants so the controller lookup can read them.
    """
    wanted = {g.vault for plan in plans for g in plan} | set(stores)
    if with_ops:
        wanted |= {g.vault for g in ops_grants()}
    return sorted(wanted - {VAULT_ALL})


def commands(  # the script's inputs, each one a fact
    server: str,
    hosts: Sequence[str],
    agents: Sequence[str],
    controllers: Collection[str],
    when: str,
    *,
    account: str,
    ops_hosts: Sequence[str],
    session_host: str | None = None,
    runner_vms: Sequence[str] = (),
    dry_run: bool,
) -> str:
    """The POSIX shell script that creates the tokens of HOSTS.

    Each of OPS_HOSTS, which are operator Macs, also gets an ops-controller
    token. SESSION_HOST, an operator Mac, gets an ops-session token, which
    goes into the keychain of the user who runs the script. Each of
    RUNNER_VMS, which are CI runner VMs, gets a token that reads only
    VAULT_RUNNERS, stored in its lab node's vault. WHEN is the UTC
    stamp in each token's name. With RUNNER_VMS and no HOSTS and no
    SESSION_HOST, the script makes only the runner-VM tokens, with no host-0-shared
    vault and no agent-host section. Every `op` command
    names ACCOUNT, the 1Password account, so none falls to op's default. With
    DRY_RUN, each `op` and `ssh` line is echoed instead of run, and nothing
    is skipped.
    """
    plans = {h: grants(h, agents, controllers) for h in hosts}
    session = (
        ()
        if session_host is None
        else ((session_host, session_grants(session_host, agents, controllers)),)
    )
    reached = [
        *plans.values(),
        *(plan for _, plan in session),
        *(runner_grants() for _ in runner_vms),
    ]
    vaults = _granted(
        reached,
        with_ops=bool(ops_hosts),
        stores=[vault_of(node_of(vm)) for vm in runner_vms],
    )
    only_runners = bool(runner_vms) and not hosts and session_host is None
    say = _echoed if dry_run else _live
    where = f"--server {shlex.quote(server)}"
    acct = f"--account {shlex.quote(account)}"
    lines = _header(
        server,
        with_ops=bool(ops_hosts),
        session=session_host,
        runners=bool(runner_vms),
        runners_only=only_runners,
        dry_run=dry_run,
    )
    wanted = vaults if only_runners else [VAULT_ALL, *vaults]
    lines += [*vault_block(acct, wanted, say, dry_run=dry_run), ""]
    lines += [
        say(f"op connect vault grant {acct} {where} --vault {shlex.quote(v)}")
        for v in wanted
    ]
    for host in hosts:
        lines += stored_block(
            host,
            store=vault_of(host),
            item=ITEM,
            token=token_name(PREFIX, host, when),
            plan=plans[host],
            acct=acct,
            where=where,
            say=say,
            dry_run=dry_run,
        )
    for vm in runner_vms:
        lines += stored_block(
            vm,
            store=vault_of(node_of(vm)),
            item=RUNNER_ITEM,
            token=token_name(RUNNER_PREFIX, vm, when),
            plan=runner_grants(),
            acct=acct,
            where=where,
            say=say,
            dry_run=dry_run,
            label=f"{RUNNER_PREFIX} {vm}",
        )
    for host in ops_hosts:
        lines += keychain_block(
            host,
            server,
            acct,
            when=when,
            plan=ops_grants(),
            say=say,
            dry_run=dry_run,
            remote=True,
        )
    for host, plan in session:
        lines += keychain_block(
            host,
            server,
            acct,
            when=when,
            plan=plan,
            say=say,
            dry_run=dry_run,
            remote=False,
        )
    return "\n".join(lines) + "\n"
