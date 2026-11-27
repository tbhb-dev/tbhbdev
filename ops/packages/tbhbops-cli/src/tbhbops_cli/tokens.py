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
from dataclasses import dataclass
from datetime import UTC
lazy from collections.abc import Callable, Collection, Iterable, Sequence
lazy from datetime import datetime

from tbhbops_cli.common import CliError

PREFIX = "agent-host"
ITEM = "connect-token"
FIELD = "token"
CATEGORY = "API_CREDENTIAL"
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
# What `tbhbdev secret read` looks up: the keychain, service and account are
# fixed in tbhbdev.secrets, and `ops` has its own copy of each.
OPS_USER = "ops"
KEYCHAIN = "ops-connect.keychain"
SERVICE = "ops-connect"
ACCOUNT = "agent"
# The operator's shell expands HOME and hostname when the script runs.
# _ssh inserts these options directly; only the remote command is _quote'd.
SSH_OPTIONS = (
    "-o IdentityAgent=none -o IdentitiesOnly=yes"
    ' -i "$HOME/.ssh/sessions/$(hostname -s)_ed25519"'
)
# security exits 44 when a search finds no item.
NOT_FOUND = 44
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


# jq turns the token on stdin into the item template `op item create -` reads.
def item_template(item: str) -> str:
    """The jq program that wraps the token on stdin as the item ITEM."""
    return (
        f'{{title: "{item}", category: "{CATEGORY}", '
        f'fields: [{{label: "{FIELD}", type: "CONCEALED", value: .}}]}}'
    )


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
            message = f"refusing to grant {grant.vault}: no token reaches host-storage-*"
            raise CliError(message)
    return found


def ops_grants() -> tuple[Grant, ...]:
    """What an ops-controller token reaches: both host-storage-* vaults, read-only."""
    return tuple(Grant(f"{FORBIDDEN}{n}", READ) for n in NAS)


def ops_controller(host: str, controllers: Collection[str]) -> tuple[Grant, ...]:
    """The ops-controller grants of HOST, which must be an operator Mac."""
    if host not in controllers:
        message = f"not an operator Mac in the inventory: {host}"
        raise CliError(message)
    return ops_grants()


def session_grants(
    host: str, agents: Sequence[str], controllers: Collection[str]
) -> tuple[Grant, ...]:
    """The ops-session grants of HOST, which must be an operator Mac.

    They are an operator Mac's own grants plus read on VAULT_OAUTH, so they
    never reach a host-storage-* vault.
    """
    if host not in controllers:
        message = f"not an operator Mac in the inventory: {host}"
        raise CliError(message)
    return (*grants(host, agents, controllers), Grant(VAULT_OAUTH, READ))


def _quote(text: str) -> str:
    """TEXT in single quotes, whatever it holds."""
    return "'" + text.replace("'", "'\\''") + "'"


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


def _indent(lines: Sequence[str], width: int) -> list[str]:
    """LINES, each one indented by WIDTH spaces."""
    return [" " * width + line for line in lines]


def _ssh(host: str, command: str, *, quiet: bool = False) -> str:
    """The ssh line that runs COMMAND as ops on HOST.

    With QUIET, ssh reads no stdin, so the line never reads the script.
    """
    flag = "-n " if quiet else ""
    return f"ssh {flag}{SSH_OPTIONS} {OPS_USER}@{host} {_quote(command)}"


def _ops_block(  # noqa: PLR0913  # the block's inputs, each one a fact
    host: str,
    server: str,
    acct: str,
    when: str,
    say: Callable[[str], str],
    *,
    dry_run: bool,
) -> list[str]:
    """The lines that make HOST's ops-controller token and keychain it as ops.

    A token that can't be stored is deleted from SERVER again.
    """
    host = shlex.quote(host)
    where = f"--server {shlex.quote(server)}"
    grant = ops_grants()
    token = token_name(OPS_PREFIX, host, when)
    name = _quote(token)
    options = " ".join(vault_option(g) for g in grant)
    # The script goes in on stdin, so no quote nests inside another.
    prepare = f"echo {_quote(PREPARE)} | {_ssh(host, 'sh -s')}"
    look = _ssh(host, LOOK, quiet=True)
    create = f"tok=$(op connect token create {acct} {name} {where} {options})"
    store = f'echo "{STORE_LINE}" | {_ssh(host, STORE_COMMAND)}'
    delete = f"op connect token delete {acct} {name} {where}"
    deleted = _quote(f"deleted the {OPS_PREFIX} token {token} from {server}")
    kept = _quote(
        f"could not delete the {OPS_PREFIX} token {token};"
        " delete it by hand with op connect token delete"
    )
    lines = ["", f"# {OPS_PREFIX} {host}"]
    if dry_run:
        return [
            *lines,
            f"# Live, it prepares the {OPS_USER} keychain {KEYCHAIN} on {host}:",
            say(prepare),
            f"# Live, it skips this Mac if {OPS_USER} holds {SERVICE}:",
            say(look),
            say(create),
            "# Live, it stops with exit 1 if the token is empty, odd or too long:",
            "# Live, it stores the token in one ssh session that unlocks the keychain:",
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
        (
            "  echo 'op connect token create printed no token for"
            f" {OPS_PREFIX} {host}' >&2"
        ),
        "  exit 1",
        "fi",
        "case $tok in",
        f"  *[!{CHARSET}]*)",
        (
            f"    echo 'the {OPS_PREFIX} token for {host} has a character the"
            " keychain line cannot carry' >&2"
        ),
        "    exit 1",
        "    ;;",
        "esac",
        "stored=false",
        f'if [ "${{#tok}}" -gt {TOKEN_MAX} ]; then',
        (
            f'  echo "the {OPS_PREFIX} token for {host} has ${{#tok}} characters,'
            f' over the {TOKEN_MAX} the keychain line can carry" >&2'
        ),
        f"elif {store} >/dev/null 2>&1; then",
        "  stored=true",
        "else",
        f"  echo 'the {OPS_PREFIX} token for {host} was made but not stored' >&2",
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
        f"  echo 'the {OPS_USER} keychain on {host} does not hold the token' >&2",
        "  exit 1",
        "fi",
    ]
    return [
        *lines,
        f"if ! {prepare}; then",
        f"  echo 'could not prepare the {OPS_USER} keychain on {host}' >&2",
        "  exit 1",
        "fi",
        "rc=0",
        f"{look} >/dev/null 2>&1 || rc=$?",
        "case $rc in",
        "  0)",
        f"    echo '{OPS_USER} on {host} already holds {SERVICE}; skipping' >&2",
        "    ;;",
        f"  {NOT_FOUND})",
        *_indent(made, 4),
        "    ;;",
        "  *)",
        f"    echo 'could not look in the {OPS_USER} keychain on {host}' >&2",
        "    exit 1",
        "    ;;",
        "esac",
    ]


def _session_block(  # noqa: PLR0913  # the block's inputs, each one a fact
    host: str,
    server: str,
    acct: str,
    *,
    when: str,
    plan: Sequence[Grant],
    say: Callable[[str], str],
    dry_run: bool,
) -> list[str]:
    """The lines that make HOST's ops-session token and keychain it, with no ssh.

    The keychain is the one of the user who runs the script. A token that
    can't be stored is deleted from SERVER again.
    """
    host = shlex.quote(host)
    where = f"--server {shlex.quote(server)}"
    token = token_name(SESSION_PREFIX, host, when)
    name = _quote(token)
    options = " ".join(vault_option(g) for g in plan)
    prepare = f"sh -c {_quote(PREPARE)}"
    look = LOOK
    create = f"tok=$(op connect token create {acct} {name} {where} {options})"
    # The braces keep the pipe on security -i, not on the unlock before it.
    store = f'echo "{STORE_LINE}" | {{ {STORE_COMMAND}; }}'
    delete = f"op connect token delete {acct} {name} {where}"
    deleted = _quote(f"deleted the {SESSION_PREFIX} token {token} from {server}")
    kept = _quote(
        f"could not delete the {SESSION_PREFIX} token {token};"
        " delete it by hand with op connect token delete"
    )
    lines = ["", f"# {SESSION_PREFIX} {host}"]
    if dry_run:
        return [
            *lines,
            f"# Live, it prepares your keychain {KEYCHAIN} on {host}:",
            say(prepare),
            f"# Live, it skips this Mac if your keychain holds {SERVICE}:",
            say(look),
            say(create),
            "# Live, it stops with exit 1 if the token is empty, odd or too long:",
            "# Live, it stores the token in one command that unlocks the keychain:",
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
        (
            "  echo 'op connect token create printed no token for"
            f" {SESSION_PREFIX} {host}' >&2"
        ),
        "  exit 1",
        "fi",
        "case $tok in",
        f"  *[!{CHARSET}]*)",
        (
            f"    echo 'the {SESSION_PREFIX} token for {host} has a character the"
            " keychain line cannot carry' >&2"
        ),
        "    exit 1",
        "    ;;",
        "esac",
        "stored=false",
        f'if [ "${{#tok}}" -gt {TOKEN_MAX} ]; then',
        (
            f'  echo "the {SESSION_PREFIX} token for {host} has ${{#tok}} characters,'
            f' over the {TOKEN_MAX} the keychain line can carry" >&2'
        ),
        f"elif {store} >/dev/null 2>&1; then",
        "  stored=true",
        "else",
        f"  echo 'the {SESSION_PREFIX} token for {host} was made but not stored' >&2",
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
        f"  echo 'your keychain on {host} does not hold the token' >&2",
        "  exit 1",
        "fi",
    ]
    return [
        *lines,
        f"if ! {prepare}; then",
        f"  echo 'could not prepare your keychain on {host}' >&2",
        "  exit 1",
        "fi",
        "rc=0",
        f"{look} >/dev/null 2>&1 || rc=$?",
        "case $rc in",
        "  0)",
        f"    echo 'your keychain on {host} already holds {SERVICE}; skipping' >&2",
        "    ;;",
        f"  {NOT_FOUND})",
        *_indent(made, 4),
        "    ;;",
        "  *)",
        f"    echo 'could not look in your keychain on {host}' >&2",
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
            f' | jq -r --arg name "$name" {_quote(VAULT_JQ)}) || {{'
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


def _stored_block(  # noqa: PLR0913  # the block's inputs, each one a fact
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
    name = _quote(token)
    options = " ".join(vault_option(g) for g in plan)
    get = f"op item get {acct} {item} --vault {own}"
    create = f"tok=$(op connect token create {acct} {name} {where} {options})"
    feed = [
        "printf '%s' \"$tok\"",
        f"jq -R {_quote(item_template(item))}",
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
        *_indent(made, 2),
        "fi",
    ]


def _header(  # noqa: PLR0913  # the header's inputs, each one a fact
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


def commands(  # noqa: PLR0913  # the script's inputs, each one a fact
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
        lines += _ops_block(host, server, acct, when, say, dry_run=dry_run)
    for host, plan in session:
        lines += _session_block(
            host, server, acct, when=when, plan=plan, say=say, dry_run=dry_run
        )
    return "\n".join(lines) + "\n"


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
