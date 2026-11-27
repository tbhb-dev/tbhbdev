"""Create a standalone run clone and prepare its Git metadata."""

from __future__ import annotations

lazy from pathlib import Path

from tbhbdev.alpha.agent import worktree_git
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid, Profile


def clone_run(mirror: Path, workspace: Path, url: str, profile: Profile) -> None:
    # A copying transport avoids shared object inodes and readable alternates.
    workspace.mkdir(mode=0o700)
    worktree_git.git(
        mirror,
        "clone",
        "--no-local",
        "--no-hardlinks",
        "--no-checkout",
        "--template=",
        str(mirror),
        str(workspace),
    )
    worktree_git.git(workspace, "config", "remote.origin.pushurl", url)
    worktree_git.git(
        workspace, "config", "core.hooksPath", str(workspace / ".git" / "hooks")
    )
    if profile is Profile.BUILD:
        # Reset inherited helpers so Git uses the run's GH_CONFIG_DIR.
        helper = f"credential.https://{repo.DEFAULT_GIT_HOST}.helper"
        worktree_git.git(workspace, "config", "--add", helper, "")
        worktree_git.git(
            workspace, "config", "--add", helper, "!gh auth git-credential"
        )


def prepare_git(workspace: Path) -> None:
    directory = workspace / ".git"
    for name, content in (
        ("config.worktree", ""),
        ("info", None),
        ("modules", None),
        ("commondir", ".\n"),
    ):
        entry = directory / name
        if entry.is_symlink():
            message = f"unexpected git metadata: {name}"
            raise Invalid(message)
        match content:
            case None:
                if not entry.exists():
                    entry.mkdir()
                valid = entry.is_dir() and next(entry.iterdir(), None) is None
            case _:
                if not entry.exists():
                    entry.write_text(content)
                valid = entry.is_file() and entry.read_text() == content
        if not valid:
            message = f"unexpected git metadata: {name}"
            raise Invalid(message)
