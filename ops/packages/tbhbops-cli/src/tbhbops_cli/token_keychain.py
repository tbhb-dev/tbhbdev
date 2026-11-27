"""Generate local and remote keychain commands for Connect tokens."""

import shlex
lazy from collections.abc import Callable, Sequence

from tbhbops_cli.token_policy import (
    OPS_PREFIX,
    SESSION_PREFIX,
    Grant,
    token_name,
    vault_option,
)
from tbhbops_cli.token_shell import indent, quote

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


def _ssh(host: str, command: str, *, quiet: bool = False) -> str:
    """The ssh line that runs COMMAND as ops on HOST.

    With QUIET, ssh reads no stdin, so the line never reads the script.
    """
    flag = "-n " if quiet else ""
    return f"ssh {flag}{SSH_OPTIONS} {OPS_USER}@{host} {quote(command)}"


def keychain_block(
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
