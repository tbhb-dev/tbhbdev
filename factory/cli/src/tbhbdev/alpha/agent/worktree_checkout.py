"""Coordinate checkout of a fresh workspace from the host mirror."""

from __future__ import annotations

import contextlib
import shutil
import stat
import subprocess  # nosec B404
from contextlib import ExitStack
from pathlib import Path
lazy from collections.abc import Callable, Mapping

from tbhbdev.alpha.agent import (
    host_tools,
    runfiles,
    worktree_clone,
    worktree_errors,
    worktree_git,
    worktree_hooks,
    worktree_mirror,
)
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions.request import check_fetch_refs
from tbhbdev.alpha.agent.model import Invalid, Profile, RunRequest


def discard_unlaunched(workspace: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (workspace / ".git/hooks").chmod(stat.S_IRWXU)
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(workspace)


def ensure_clone_directory(clone: Path) -> None:
    if any(p.is_symlink() for p in (clone, *clone.parents[:3])):
        message = "repo clone paths must not be symbolic links"
        raise Invalid(message)
    with worktree_errors.preparation_step("repo clone directory"):
        # The run's clone is standalone, so the account's own clone is only a parent.
        clone.mkdir(parents=True, exist_ok=True)


def create(
    clone: Path,
    name: str,
    request: RunRequest,
    token_source: Callable[[], str | None] | None = None,
    *,
    hook_env: Mapping[str, str] | None = None,
) -> Path:
    check_fetch_refs(request.fetch_refs)
    host, owner, repository = repo.parts(request.repo)
    if (host, owner.lower()) != (repo.DEFAULT_GIT_HOST, repo.DEFAULT_ORG):
        message = (
            f"repository {request.repo}: agent mirrors fetch only "
            f"{repo.DEFAULT_ORG} repositories"
        )
        raise Invalid(message)
    url = f"https://github.com/{owner}/{repository}.git"
    ensure_clone_directory(clone)
    worktree = clone / ".worktrees" / f"agent-{name}"
    if worktree.parent.is_symlink():
        message = "run clone parent must not be a symbolic link"
        raise Invalid(message)
    mirror = worktree_mirror.mirror_path(Path.home(), request.repo)
    if any(p.is_symlink() for p in (mirror, *mirror.parents[:3])):
        message = "host mirror paths must not be symbolic links"
        raise Invalid(message)
    with ExitStack() as stack:
        discard = stack.enter_context(ExitStack())
        with worktree_errors.preparation_step("mirror directory"):
            mirror = runfiles.private_dir(mirror)
            stack.enter_context(runfiles.locked(mirror / "agent-mirror.lock"))
        with worktree_errors.preparation_step("mirror init"):
            worktree_mirror.configure_mirror(mirror, url)
        with worktree_errors.preparation_step("clone"):
            worktree.parent.mkdir(exist_ok=True)
            worktree_clone.clone_run(mirror, worktree, url, request.profile)
        discard.callback(discard_unlaunched, worktree)
        if hook_env is not None:
            with worktree_errors.preparation_step("hook verification"):
                worktree_hooks.verify_run_hooks(worktree, hook_env)
        ref_details = (
            f"; requested refs: {', '.join(request.fetch_refs)}"
            if request.fetch_refs
            else ""
        )
        with worktree_errors.preparation_step(f"mirror fetch ({url}){ref_details}"):
            try:
                commit = worktree_mirror.fetch_commit(mirror, request)
            except subprocess.SubprocessError:
                # A private repository refuses the anonymous fetch. Retry over
                # https with the run's App token. With no token the fetch fails:
                # an ssh retry could follow a transfer to another owner.
                token = token_source() if token_source is not None else None
                if token is None:
                    raise
                commit = worktree_mirror.fetch_commit(mirror, request, token)
        worktree_hooks.prepare_hooks(
            worktree, name, commit, hook_env, request.start_ref
        )
        with worktree_errors.preparation_step("checkout"):
            worktree_git.git(
                worktree, "fetch", "--no-tags", "--no-write-fetch-head",
                "--no-recurse-submodules", str(mirror),
                "+refs/heads/*:refs/remotes/origin/*",
            )  # fmt: skip
            if request.profile is Profile.BUILD:
                worktree_git.git(
                    worktree, "checkout", "-b", f"agent/{name}", commit, "--"
                )
            else:
                worktree_git.git(worktree, "checkout", "--detach", commit, "--")
        with worktree_errors.preparation_step("git metadata"):
            worktree_clone.prepare_git(worktree)
        if request.profile is Profile.BUILD:
            # A review never commits, so the tools its commit hooks use may differ.
            host_tools.require_check_tools(worktree, hook_env)
        discard.pop_all()
    return worktree
