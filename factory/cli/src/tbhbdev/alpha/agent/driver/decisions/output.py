"""Format coordinator status and safety messages."""

from __future__ import annotations

from pathlib import PurePosixPath

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.terminal import terminal_text

INSPECTION_WARNING = (
    "Don't run git inside a run clone: it is agent-written. "
    "Use `tbhbdev alpha agent log|diff NAME`."
)


def sandbox(authorized_by: str | None) -> str:
    if authorized_by is None:
        return ""
    return f" sandbox=none authorized_by={terminal_text(authorized_by)}"


def launched(
    name: str,
    host: str,
    account: str,
    *,
    authorized_by: str | None = None,
) -> str:
    return f"{name}: launched host={host} account={account}" + sandbox(authorized_by)


def status(
    name: str,
    *,
    phase: str,
    rc: str,
    target: tuple[str, str, str | None, str | None],
    started: str,
) -> str:
    host, account, authorized_by, launcher = target
    return (
        f"{name}: phase={phase} rc={rc} host={host} account={account} started={started}"
        f" launcher={launcher or 'unknown'}"
    ) + sandbox(authorized_by)


def worktree_kept(name: str, host: str, repository: str) -> str:
    tree = repo.clone_path(PurePosixPath("."), repository)
    return f"{name}: worktree kept at {host}:~/{tree}/.worktrees/agent-{name}"
