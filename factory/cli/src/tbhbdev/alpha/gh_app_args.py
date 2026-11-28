"""Parse GitHub App command arguments and validate their scope."""

import argparse
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, override

if TYPE_CHECKING:
    from collections.abc import Sequence

APP_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")
REPO_PATTERN = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
# Eleven digits reach the year 5138, which datetime still formats.
EPOCH_PATTERN = re.compile(r"[0-9]{1,11}", re.ASCII)
PERMISSION_PATTERN = re.compile(r"[a-z][a-z_]*:(?:read|write)", re.ASCII)
# GitHub's repository permission names, so a typo fails before any network call.
PERMISSION_NAMES = frozenset(
    {
        "actions",
        "administration",
        "checks",
        "codespaces",
        "contents",
        "dependabot_secrets",
        "deployments",
        "discussions",
        "environments",
        "issues",
        "merge_queues",
        "metadata",
        "packages",
        "pages",
        "pull_requests",
        "repository_advisories",
        "repository_hooks",
        "repository_projects",
        "secret_scanning_alerts",
        "secrets",
        "security_events",
        "single_file",
        "statuses",
        "vulnerability_alerts",
        "workflows",
    }
)
DEFAULT_VAULT = "tbhb-dev"
NOT_A_PART = (".", "..")
USAGE = """usage: {prog} APP OWNER/REPO [--fresh] [--dry-run] [--all-repos] [--vault V]
                                 [--permissions NAME:LEVEL,...]
       {prog} APP --cooldown-until EPOCH
       {prog} APP --cooldown-status
"""


class UsageError(Exception):
    """The command line is wrong."""


class HelpRequestedError(Exception):
    """The command line asked for the usage text."""


@dataclass(frozen=True)
class Args:
    app: str
    repo: str  # empty for a cooldown option
    fresh: bool
    dry_run: bool
    all_repos: bool
    vault: str
    cooldown_until: int | None
    cooldown_status: bool
    permissions: tuple[tuple[str, str], ...] = ()


class Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)

    @override
    def print_help(self, *_args: object, **_kwargs: object) -> None:
        raise HelpRequestedError


def parse_epoch(text: str | None) -> int | None:
    """The epoch TEXT holds, or None when it isn't whole seconds."""
    if text is None:
        return None
    stripped = text.strip()
    if not EPOCH_PATTERN.fullmatch(stripped):
        return None
    return int(stripped)


def parse_args(argv: Sequence[str]) -> Args:
    """Parse ARGV.

    Raises:
        HelpRequestedError: ARGV holds -h or --help.
        UsageError: An argument or a combination of them is wrong.
    """
    parser = Parser()
    parser.add_argument("app")
    parser.add_argument("repo", nargs="?")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all-repos", action="store_true")
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--permissions", action="append")
    parser.add_argument("--cooldown-until")
    parser.add_argument("--cooldown-status", action="store_true")
    ns = parser.parse_args(argv)
    if not APP_PATTERN.fullmatch(ns.app):
        message = f"app {ns.app!r} must match {APP_PATTERN.pattern}"
        raise UsageError(message)
    until = None
    if ns.cooldown_until is not None:
        until = parse_epoch(ns.cooldown_until)
        if until is None:
            message = "--cooldown-until needs a whole number of seconds since the epoch"
            raise UsageError(message)
    if until is not None and ns.cooldown_status:
        message = "--cooldown-until and --cooldown-status are exclusive"
        raise UsageError(message)
    if until is not None or ns.cooldown_status:
        if (
            ns.repo is not None
            or ns.fresh
            or ns.dry_run
            or ns.all_repos
            or ns.permissions is not None
        ):
            message = "a cooldown option takes only the app"
            raise UsageError(message)
    elif ns.repo is None:
        message = "OWNER/REPO is required"
        raise UsageError(message)
    elif not valid_repo(ns.repo):
        message = f"repository {ns.repo!r} must be OWNER/REPO"
        raise UsageError(message)
    if not valid_vault(ns.vault):
        message = f"vault {ns.vault!r} cannot be a directory name"
        raise UsageError(message)
    permissions = parse_permissions(ns.permissions)
    return Args(
        ns.app,
        ns.repo or "",
        ns.fresh,
        ns.dry_run,
        ns.all_repos,
        ns.vault,
        until,
        ns.cooldown_status,
        permissions,
    )


def parse_permissions(values: list[str] | None) -> tuple[tuple[str, str], ...]:
    """NAME:LEVEL pairs from the option's one comma list, sorted; empty for none.

    Raises:
        UsageError: The option repeats, an entry isn't NAME:read or NAME:write
            with a known name, or a name repeats.
    """
    if values is None:
        return ()
    if len(values) > 1:
        message = "--permissions can be given once"
        raise UsageError(message)
    text = values[0]
    pairs: dict[str, str] = {}
    for entry in text.split(","):
        name, _, level = entry.partition(":")
        if (
            PERMISSION_PATTERN.fullmatch(entry) is None
            or name not in PERMISSION_NAMES
            or name in pairs
        ):
            message = (
                f"--permissions entry {entry!r} must be a known NAME:read "
                "or NAME:write, once"
            )
            raise UsageError(message)
        pairs[name] = level
    return tuple(sorted(pairs.items()))


def valid_repo(repo: str) -> bool:
    """Whether REPO is OWNER/REPO, with no part that climbs or stays in a path."""
    parts = repo.split("/")
    return REPO_PATTERN.fullmatch(repo) is not None and not set(parts) & set(NOT_A_PART)


def valid_vault(vault: str) -> bool:
    """Whether VAULT can be one component of a path."""
    return vault not in ("", *NOT_A_PART) and "/" not in vault and "\0" not in vault


def split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name
