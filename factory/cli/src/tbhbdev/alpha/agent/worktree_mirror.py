"""Configure a host-owned mirror and fetch the requested refs."""

from __future__ import annotations

import re
import subprocess  # nosec B404
lazy from pathlib import Path

from tbhbdev.alpha.agent import runfiles, worktree_git
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions.request import check_fetch_refs, check_start_ref
from tbhbdev.alpha.agent.model import Invalid, Profile, RunRequest

MIRROR_CONFIG = """\
[core]
repositoryformatversion = 0
bare = true
logAllRefUpdates = false
[gc]
auto = 0
pruneExpire = never
[maintenance]
auto = false
[fetch]
recurseSubmodules = false
[submodule]
recurse = false
[remote "origin"]
url = {url}
"""


def mirror_path(home: Path, argument: str) -> Path:
    host, owner, name = repo.parts(argument)
    state = runfiles.state_home(home)
    return state / "tbhbdev" / "agent-mirrors" / host.lower() / owner / f"{name}.git"


def configure_mirror(mirror: Path, url: str) -> None:
    if not (mirror / "config").exists():
        worktree_git.git(mirror, "init", "--bare", "--template=")
    # All values come from validated repository names, never a checkout.
    runfiles.atomic(mirror / "config", MIRROR_CONFIG.format(url=url))


def default_branch(mirror: Path, token: str | None = None) -> str:
    query = ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD")
    listing = (
        worktree_git.git(mirror, *query)
        if token is None
        else worktree_git.git(mirror, *query, token=token)
    )
    for line in listing.splitlines():
        if match := re.fullmatch(r"ref: (refs/heads/[^\s]+)\tHEAD", line):
            worktree_git.git(
                mirror,
                "check-ref-format",
                "--branch",
                match[1].removeprefix("refs/heads/"),
            )
            return match[1]
    message = "origin HEAD does not name a branch"
    raise Invalid(message)


def fetched_ref(ref: str) -> str:
    return f"refs/heads/fetched/{ref.removeprefix('refs/')}"


def start_reference(mirror: Path, ref: str, default: str) -> str:
    try:
        worktree_git.git(mirror, "check-ref-format", ref)
    except subprocess.CalledProcessError:
        message = f"--start-ref is not a valid Git ref: {ref!r}"
        raise Invalid(message) from None
    if ref == default:
        message = f"--start-ref cannot name the default branch: {ref}"
        raise Invalid(message)
    return fetched_ref(ref)


def fetch_commit(mirror: Path, request: RunRequest, token: str | None = None) -> str:
    check_start_ref(request)
    check_fetch_refs(request.fetch_refs)
    options = (
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--no-tags",
        "--upload-pack=git-upload-pack",
    )
    branch = default_branch(mirror, token)
    reference = branch if request.profile is Profile.BUILD else request.commit
    if request.start_ref is not None:
        reference = start_reference(mirror, request.start_ref, branch)
    references = [f"+{branch}:{branch}"]
    references.extend(f"+{ref}:{fetched_ref(ref)}" for ref in request.fetch_refs)
    if request.commit is not None:
        references.append(f"+{request.commit}:refs/heads/tbhbdev-review")
    fetch = ("fetch", *options, "--no-prune", "origin", *references)
    if token is None:
        worktree_git.git(mirror, *fetch)
    else:
        worktree_git.git(mirror, *fetch, token=token)
    commit = worktree_git.git(
        mirror, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"
    )
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit) is None:
        message = "mirror did not resolve a full commit"
        raise Invalid(message)
    return commit
