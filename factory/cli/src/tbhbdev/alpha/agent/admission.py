"""Check and reserve host-wide launch slots."""

from __future__ import annotations

import argparse
import fcntl
import os
import pwd
import stat
import sys
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import Host

ROOT = Path("/var/tmp/tbhbdev-agent-admission")  # noqa: S108  # nosec B108  # Sticky directory with no-follow opens and slot owner and mode checks.
SLOT_FULL = "full"
_DIRECTORY_MODE = 0o1777


class SlotFullError(Invalid):
    """No slot is available to the requested profile."""


def _owner_ids(accounts: tuple[str, ...]) -> set[int]:
    user_ids = {0, os.getuid()}
    for account in accounts:
        with suppress(KeyError):
            user_ids.add(pwd.getpwnam(account).pw_uid)
    return user_ids


@contextmanager
def _directory(root: Path) -> Generator[int]:
    with suppress(FileExistsError):
        root.mkdir(mode=_DIRECTORY_MODE)
    fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        # mkdir applies the umask, so another launch can see the new directory
        # before its creator widens it, or after the creator died. Widen a
        # directory this account owns instead of refusing.
        if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE and info.st_uid == os.getuid():
            os.fchmod(fd, _DIRECTORY_MODE)
        if stat.S_IMODE(os.fstat(fd).st_mode) != _DIRECTORY_MODE:
            message = f"agent admission directory must have mode {_DIRECTORY_MODE:o}"
            raise Invalid(message)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _slot(
    directory: int, name: str, owner_ids: set[int], *, create: bool = False
) -> Generator[int]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    # Open existing files without O_CREAT so Linux's sticky-directory
    # protection lets us inspect their ownership.
    if create:
        with suppress(FileExistsError):
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=directory)
            try:
                os.fchmod(fd, 0o644)
            finally:
                os.close(fd)
    fd = os.open(name, flags, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            message = "agent admission slot must be a regular file"
            raise Invalid(message)
        if info.st_uid not in owner_ids:
            message = (
                "agent admission slot must be owned by the current uid, root, "
                "or a configured agent account"
            )
            raise Invalid(message)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            message = "agent admission slot must not be group- or other-writable"
            raise Invalid(message)
        yield fd
    finally:
        os.close(fd)


def _active(directory: int, profile: Profile, owner_ids: set[int]) -> set[str]:
    active: set[str] = set()
    for name in os.listdir(directory):
        if name.startswith(f"{profile}-"):
            with _slot(directory, name, owner_ids) as fd:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    active.add(name)
    return active


@contextmanager
def _available(
    root: Path,
    host_name: str,
    profile: Profile,
    limit: int | None,
    owner_ids: set[int],
) -> Generator[tuple[int, set[str]]]:
    if limit is None:
        message = f"{host_name}: the request carries no max_concurrent_runs"
        raise Invalid(message)
    with (
        _directory(root) as directory,
        _slot(directory, "admission.lock", owner_ids, create=True) as guard,
    ):
        fcntl.flock(guard, fcntl.LOCK_EX)
        builds = _active(directory, Profile.BUILD, owner_ids)
        reviews = _active(directory, Profile.REVIEW, owner_ids)
        if len(builds) + len(reviews) >= limit:
            message = (
                f"{host_name}: no agent slot available (max_concurrent_runs={limit})"
            )
            raise SlotFullError(message)
        if profile is Profile.BUILD and len(builds) >= max(limit - 1, 1):
            message = f"{host_name}: remaining agent capacity is reserved for reviews"
            raise SlotFullError(message)
        yield directory, builds if profile is Profile.BUILD else reviews


@contextmanager
def reserve(
    root: Path, host: Host, profile: Profile, limit: int | None
) -> Generator[None]:
    """Check both slot counts and claim capacity under one shared lock.

    LIMIT is the launcher's ``max_concurrent_runs`` for the host. The run
    host's own account config can lack the host, so its default must never
    decide the cap.

    Raises:
        Invalid: LIMIT is None, or no slot is free.
    """
    owner_ids = _owner_ids(host.accounts)
    with ExitStack() as slot:
        with _available(root, host.name, profile, limit, owner_ids) as (
            directory,
            active,
        ):
            names = (f"{profile}-{i}" for i in range(len(active) + 1))
            name = next(candidate for candidate in names if candidate not in active)
            fd = slot.enter_context(_slot(directory, name, owner_ids, create=True))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main(argv: list[str], prog: str) -> int:
    """Report whether the launching profile can claim a slot on this host."""
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("host")
    parser.add_argument("profile", type=Profile, choices=list(Profile))
    parser.add_argument("limit", type=int)
    parser.add_argument("accounts", nargs="*")
    try:
        args = parser.parse_args(argv)
        owner_ids = _owner_ids(tuple(args.accounts))
        with _available(ROOT, args.host, args.profile, args.limit, owner_ids):
            return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except SlotFullError:
        print(SLOT_FULL)
        return 0
    except (Invalid, OSError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
