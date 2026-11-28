"""Persist GitHub App token and cooldown state on disk."""

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.gh_app_args import parse_epoch, split_repo
from tbhbdev.alpha.gh_app_runtime import fill

if TYPE_CHECKING:
    from tbhbdev.alpha.gh_app_api import Minted
    from tbhbdev.alpha.gh_app_args import Args

REUSE_MARGIN = 600
ALL_REPOS_DIR = "+all"
HOSTS_FILE = "hosts.yml"
SLUG_FILE = "app-slug"
EXPIRES_FILE = "expires-at"


def out_dir(state: Path, args: Args) -> Path:
    owner, name = split_repo(args.repo)
    # A narrowed token has its own directory: `+` can't appear in a repository name.
    narrowed = "".join(f"+{n.replace('_', '-')}-{v}" for n, v in args.permissions)
    return (
        state
        / args.vault
        / args.app
        / owner
        / ((ALL_REPOS_DIR if args.all_repos else name) + narrowed)
    )


def cooldown_path(state: Path, app: str) -> Path:
    return state / "cooldown" / app


def cooldown_until(text: str | None, now: int) -> int | None:
    """The epoch an active cooldown lasts until, or None when there isn't one."""
    until = parse_epoch(text)
    if until is None or until <= now:
        return None
    return until


def reusable(expires: str | None, now: int, *, fresh: bool) -> bool:
    if fresh:
        return False
    epoch = parse_epoch(expires)
    return epoch is not None and epoch - now > REUSE_MARGIN


def utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cooldown_message(until: int) -> str:
    return f"cooldown until {utc(until)}"


def render_hosts(app: str, token: str) -> str:
    user = f'"{app}[bot]"'
    return (
        "github.com:\n"
        "    users:\n"
        f"        {user}:\n"
        f"            oauth_token: {token}\n"
        "    git_protocol: https\n"
        f"    oauth_token: {token}\n"
        f"    user: {user}\n"
    )


def read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode()
    except OSError, UnicodeDecodeError:
        return None


def write_file(path: Path, text: str) -> None:
    """Write TEXT to PATH, owner-only, so that no reader sees it half-written."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        fill(handle, text)
        Path(temp).replace(path)
        sync_dir(path.parent)
    except BaseException:
        # After the rename the temp file is gone, and a failed cleanup must not
        # hide the error that brought us here.
        Path(temp).unlink(missing_ok=True)
        raise


def sync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def store_token(args: Args, out: Path, minted: Minted) -> None:
    make_dir(out)
    write_file(out / HOSTS_FILE, render_hosts(args.app, minted.token))
    write_file(out / SLUG_FILE, f"{args.app}\n")
    # Last, so that no reader sees a new expiry beside an old token.
    expires = 0 if args.fresh else minted.expires_at
    write_file(out / EXPIRES_FILE, f"{expires}\n")
