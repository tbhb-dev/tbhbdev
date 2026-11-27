"""Checks for the agent and ops accounts on a Synology DSM storage server.

The `dsm_accounts` role reads /etc/passwd, /etc/group and each backup share's
ACL from the server and hands the text to the Ansible filters that wrap this
module. Each function returns what is wrong, one line per gap, and an empty
list when nothing is. Nothing here changes the server.
"""

import re
lazy from collections.abc import Iterable

# Every permission an ACL entry can carry. A deny entry with all of them is
# the share-level No access that DSM shows.
ALL_PERMISSIONS = frozenset("rwxpdDaARWcCo")

# One line of `synoacltool -get`, such as `[0] user:agent:deny:rwxpdDaARWcCo:fd--`.
_ENTRY = re.compile(
    r"^\s*\[\d+\]\s+(?P<kind>user|group):(?P<name>[^:\s]+)"
    r":(?P<verdict>allow|deny):(?P<permissions>[A-Za-z-]+):"
)


def account_gaps(
    passwd: str,
    group: str,
    accounts: Iterable[str],
    groups: Iterable[str],
    shell: str,
) -> list[str]:
    """Return how the accounts fall short of the groups and shell required.

    `passwd` and `group` are the text of /etc/passwd and /etc/group. An
    account that doesn't exist gets one gap, since the rest follows from it.
    """
    shells = {
        fields[0]: fields[6]
        for line in passwd.splitlines()
        if len(fields := line.split(":")) >= 7  # noqa: PLR2004
    }
    members = {
        fields[0]: set(fields[3].split(","))
        for line in group.splitlines()
        if len(fields := line.split(":")) >= 4  # noqa: PLR2004
    }
    gaps: list[str] = []
    for account in accounts:
        if account not in shells:
            gaps.append(f"{account}: no such account")
            continue
        if shells[account] != shell:
            gaps.append(f"{account}: shell is {shells[account]}, not {shell}")
        gaps.extend(
            f"{account}: not in {name}"
            for name in groups
            if account not in members.get(name, set())
        )
    return gaps


def acl_gaps(acl: str, accounts: Iterable[str]) -> list[str]:
    """Return which accounts lack a first-place deny entry in a share's ACL.

    `acl` is the output of `synoacltool -get` for the share's path. Each
    account needs a user entry that denies every permission, ahead of every
    allow entry, because DSM applies the first entry that matches.
    """
    entries = [match for line in acl.splitlines() if (match := _ENTRY.match(line))]
    first_allow = next(
        (
            position
            for position, entry in enumerate(entries)
            if entry["verdict"] == "allow"
        ),
        len(entries),
    )
    gaps: list[str] = []
    for account in accounts:
        denies = [
            position
            for position, entry in enumerate(entries)
            if entry["kind"] == "user"
            and entry["name"] == account
            and entry["verdict"] == "deny"
            and set(entry["permissions"]) >= ALL_PERMISSIONS
        ]
        if not denies:
            gaps.append(f"{account}: no deny entry for every permission")
        elif denies[0] > first_allow:  # pragma: no mutate -- equal is impossible
            gaps.append(f"{account}: deny entry sits after an allow entry")
    return gaps
