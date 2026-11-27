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
lazy from collections.abc import Callable, Collection, Iterable, Sequence

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
    vault_option,
)
from tbhbops_cli.token_shell import indent, quote

PREFIX = "agent-host"
ITEM = "connect-token"
FIELD = "token"
CATEGORY = "API_CREDENTIAL"
# What `tbhbdev secret read` looks up: the keychain, service and account are
# fixed in tbhbdev.secrets, and `ops` has its own copy of each.
OPS_USER = "ops"
KEYCHAIN = "ops-connect.keychain"
SERVICE = "ops-connect"
ACCOUNT = "agent"
# The operator's shell expands HOME and hostname when the script runs.
# _ssh inserts these options directly; only the remote command is quoted.
SSH_OPTIONS = (
    "-o IdentityAgent=none -o IdentitiesOnly=yes"
    ' -i "$HOME/.ssh/sessions/$(hostname -s)_ed25519"'
)
# security exits 44 when a search finds no item.
NOT_FOUND = 44


# jq turns the token on stdin into the item template `op item create -` reads.
def item_template(item: str) -> str:
    """The jq program that wraps the token on stdin as the item ITEM."""
    return (
        f'{{title: "{item}", category: "{CATEGORY}", '
        f'fields: [{{label: "{FIELD}", type: "CONCEALED", value: .}}]}}'
    )


def _live(text: str) -> str:
    return text


def _echoed(text: str) -> str:
    return f"echo {shlex.quote(text)}"


PREPARE = (
    "mkdir -p ~/Library/Keychains"
    f" && {{ [ -e ~/Library/Keychains/{KEYCHAIN}-db ]"
    f' || security create-keychain -p "" {KEYCHAIN}; }}'
    f' && security unlock-keychain -p "" {KEYCHAIN}'
    f" && security set-keychain-settings {KEYCHAIN}"
)
LOOK = f"security find-generic-password -s {SERVICE} -a {ACCOUNT} {KEYCHAIN}"
# security -i reads this line from stdin, so the token never reaches an argv:
# echo is a shell builtin. The script has checked that $tok holds only
# [A-Za-z0-9._-], which needs no quoting and holds no backslash for echo.
STORE_LINE = (
    f"add-generic-password -U -s {SERVICE} -a {ACCOUNT} -T /usr/bin/security"
    f" -w $tok {KEYCHAIN}"
)
# `security -i` reads at most 4095 characters per line; measured on macOS 27, a
# longer line is split and its overflow runs as a separate command. The token
# can fill what the line leaves, so a longer one is never sent.
LINE_MAX = 4095
TOKEN_MAX = LINE_MAX - len(STORE_LINE.replace("$tok", ""))
# One ssh session unlocks the keychain and then stores, because a keychain that
# another session unlocked may be locked again in this one, with no UI to ask.
STORE_COMMAND = f'security unlock-keychain -p "" {KEYCHAIN} && security -i'
CHARSET = "A-Za-z0-9._-"
# jq tells whether the vault list on stdin holds a vault named $name, exactly.
VAULT_JQ = "any(.[]; .name == $name)"


def _ssh(host: str, command: str, *, quiet: bool = False) -> str:
    """The ssh line that runs COMMAND as ops on HOST.

    With QUIET, ssh reads no stdin, so the line never reads the script.
    """
    flag = "-n " if quiet else ""
    return f"ssh {flag}{SSH_OPTIONS} {OPS_USER}@{host} {quote(command)}"


def _keychain_block(  # the block's inputs, each one a fact
    host: str,
    server: str,
    acct: str,
    *,
    when: str,
    plan: Sequence[Grant],
    say: Callable[[str], str],
    dry_run: bool,
    remote: bool,
) -> list[str]:
    """Make HOST's token and store it in the local or remote keychain.

    Delete a token from SERVER if the keychain cannot store it.
    """
    host = shlex.quote(host)
    where = f"--server {shlex.quote(server)}"
    prefix = OPS_PREFIX if remote else SESSION_PREFIX
    token = token_name(prefix, host, when)
    name = quote(token)
    options = " ".join(vault_option(g) for g in plan)
    if remote:
        # Pass the preparation script on stdin to avoid nested shell quoting.
        prepare = f"echo {quote(PREPARE)} | {_ssh(host, 'sh -s')}"
        look = _ssh(host, LOOK, quiet=True)
        store = f'echo "{STORE_LINE}" | {_ssh(host, STORE_COMMAND)}'
        keychain = f"the {OPS_USER} keychain"
        holder = f"{OPS_USER} on {host}"
        transport = "ssh session"
    else:
        prepare = f"sh -c {quote(PREPARE)}"
        look = LOOK
        # The braces keep the pipe on security -i, after the unlock.
        store = f'echo "{STORE_LINE}" | {{ {STORE_COMMAND}; }}'
        keychain = "your keychain"
        holder = f"{keychain} on {host}"
        transport = "command"
    create = f"tok=$(op connect token create {acct} {name} {where} {options})"
    delete = f"op connect token delete {acct} {name} {where}"
    deleted = quote(f"deleted the {prefix} token {token} from {server}")
    kept = quote(
        f"could not delete the {prefix} token {token};"
        " delete it by hand with op connect token delete"
    )
    lines = ["", f"# {prefix} {host}"]
    if dry_run:
        return [
            *lines,
            f"# Live, it prepares {keychain} {KEYCHAIN} on {host}:",
            say(prepare),
            (
                f"# Live, it skips this Mac if {OPS_USER} holds {SERVICE}:"
                if remote
                else f"# Live, it skips this Mac if {keychain} holds {SERVICE}:"
            ),
            say(look),
            say(create),
            "# Live, it stops with exit 1 if the token is empty, odd or too long:",
            f"# Live, it stores the token in one {transport} that unlocks the keychain:",
            say(store),
            "# Live, if the token is over-length or not stored, it deletes the token",
            "# and stops with exit 1:",
            say(delete),
            say("unset tok"),
            "# Live, it stops with exit 1 unless the item is there:",
            say(look),
        ]
    made = [
        create,
        'if [ -z "$tok" ]; then',
        (f"  echo 'op connect token create printed no token for {prefix} {host}' >&2"),
        "  exit 1",
        "fi",
        "case $tok in",
        f"  *[!{CHARSET}]*)",
        (
            f"    echo 'the {prefix} token for {host} has a character the"
            " keychain line cannot carry' >&2"
        ),
        "    exit 1",
        "    ;;",
        "esac",
        "stored=false",
        f'if [ "${{#tok}}" -gt {TOKEN_MAX} ]; then',
        (
            f'  echo "the {prefix} token for {host} has ${{#tok}} characters,'
            f' over the {TOKEN_MAX} the keychain line can carry" >&2'
        ),
        f"elif {store} >/dev/null 2>&1; then",
        "  stored=true",
        "else",
        f"  echo 'the {prefix} token for {host} was made but not stored' >&2",
        "fi",
        'if [ "$stored" = false ]; then',
        f"  if {delete} >/dev/null; then",
        f"    echo {deleted} >&2",
        "  else",
        f"    echo {kept} >&2",
        "  fi",
        "  exit 1",
        "fi",
        "unset tok",
        f"if ! {look} >/dev/null 2>&1; then",
        f"  echo '{keychain} on {host} does not hold the token' >&2",
        "  exit 1",
        "fi",
    ]
    return [
        *lines,
        f"if ! {prepare}; then",
        f"  echo 'could not prepare {keychain} on {host}' >&2",
        "  exit 1",
        "fi",
        "rc=0",
        f"{look} >/dev/null 2>&1 || rc=$?",
        "case $rc in",
        "  0)",
        f"    echo '{holder} already holds {SERVICE}; skipping' >&2",
        "    ;;",
        f"  {NOT_FOUND})",
        *indent(made, 4),
        "    ;;",
        "  *)",
        f"    echo 'could not look in {keychain} on {host}' >&2",
        "    exit 1",
        "    ;;",
        "esac",
    ]


def _vault_block(
    acct: str, names: Sequence[str], say: Callable[[str], str], *, dry_run: bool
) -> list[str]:
    """The lines that create each of NAMES the account lacks, from one vault list."""
    listing = f"op vault list {acct} --format json"
    if dry_run:
        return [
            "# Live, it lists the vaults once and stops with exit 1 if that fails:",
            say(listing),
            "# Live, it creates each of these vaults that the list lacks, and prints",
            "# created vault <name> for each one:",
            *(say(f"op vault create {acct} {shlex.quote(n)}") for n in names),
        ]
    return [
        f"vaults=$({listing}) || {{",
        "  echo 'could not list the vaults in the 1Password account' >&2",
        "  exit 1",
        "}",
        f"for name in {' '.join(shlex.quote(n) for n in names)}; do",
        (
            "  found=$(printf '%s' \"$vaults\""
            f' | jq -r --arg name "$name" {quote(VAULT_JQ)}) || {{'
        ),
        "    echo 'could not read the vault list' >&2",
        "    exit 1",
        "  }",
        '  if [ "$found" = false ]; then',
        f'    op vault create {acct} "$name" >/dev/null',
        '    echo "created vault $name"',
        "  fi",
        "done",
        "unset vaults",
    ]


def _stored_block(  # the block's inputs, each one a fact
    host: str,
    *,
    store: str,
    item: str,
    token: str,
    plan: Sequence[Grant],
    acct: str,
    where: str,
    say: Callable[[str], str],
    dry_run: bool,
    label: str | None = None,
) -> list[str]:
    """The lines that make HOST's token TOKEN and store it as ITEM in vault STORE.

    A STORE that already holds ITEM is skipped, so a rerun makes no token.
    """
    own = shlex.quote(store)
    name = quote(token)
    options = " ".join(vault_option(g) for g in plan)
    get = f"op item get {acct} {item} --vault {own}"
    create = f"tok=$(op connect token create {acct} {name} {where} {options})"
    feed = [
        "printf '%s' \"$tok\"",
        f"jq -R {quote(item_template(item))}",
        f"op item create {acct} --vault {own} -",
    ]
    lines = ["", f"# {label or host}"]
    if dry_run:
        return [
            *lines,
            "# Live, it skips this host if this succeeds:",
            say(get),
            say(create),
            "# Live, it stops with exit 1 if the token is empty:",
            say(" | ".join(feed)),
            say("unset tok"),
        ]
    made = [
        create,
        'if [ -z "$tok" ]; then',
        f"  echo 'op connect token create printed no token for {host}' >&2",
        "  exit 1",
        "fi",
        " \\\n    | ".join(feed),
        "unset tok",
    ]
    return [
        *lines,
        f"if {get} >/dev/null 2>&1; then",
        f"  echo '{store} already holds {item}; skipping' >&2",
        "else",
        *indent(made, 2),
        "fi",
    ]


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
    lines += [*_vault_block(acct, wanted, say, dry_run=dry_run), ""]
    lines += [
        say(f"op connect vault grant {acct} {where} --vault {shlex.quote(v)}")
        for v in wanted
    ]
    for host in hosts:
        lines += _stored_block(
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
        lines += _stored_block(
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
        lines += _keychain_block(
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
        lines += _keychain_block(
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
