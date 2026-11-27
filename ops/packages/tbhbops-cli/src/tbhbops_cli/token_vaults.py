"""Generate vault provisioning and item storage commands for Connect tokens."""

import shlex
lazy from collections.abc import Callable, Sequence

from tbhbops_cli.token_policy import Grant, vault_option
from tbhbops_cli.token_shell import indent, quote

ITEM = "connect-token"
FIELD = "token"
CATEGORY = "API_CREDENTIAL"
# jq tells whether the vault list on stdin holds a vault named $name, exactly.
VAULT_JQ = "any(.[]; .name == $name)"


def _item_template(item: str) -> str:
    """The jq program that wraps the token on stdin as the item ITEM."""
    return (
        f'{{title: "{item}", category: "{CATEGORY}", '
        f'fields: [{{label: "{FIELD}", type: "CONCEALED", value: .}}]}}'
    )


def vault_block(
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


def stored_block(
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
        f"jq -R {quote(_item_template(item))}",
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
