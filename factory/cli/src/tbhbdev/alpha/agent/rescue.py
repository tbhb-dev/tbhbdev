"""Save and publish a failed build's work from the launcher."""

from __future__ import annotations

import re
import subprocess  # nosec B404
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import secret_scan
from tbhbdev.alpha.agent import credentials, host_tools, push, worktree
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Profile
from tbhbdev.alpha.gh_app import GhAppError

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host import Prepared, Run

FAILURES = (OSError, ValueError, subprocess.SubprocessError)
FINDING = 3
SCAN_SECONDS = 60
VENDORED_RULES = "lib/rules/lint/gitleaks/base.toml"


def on_fault(run: Run, prepared: Prepared | None, rc: int) -> None:
    """Record the publication result before the launcher records completion."""
    if (
        rc == 0
        or prepared is None
        or run.request.profile is not Profile.BUILD
        or not run.files.path("launched").exists()
        or not run.files.path("base").exists()
    ):
        return
    result = (
        "kept local: harness shutdown unconfirmed"
        if run.files.uncertain
        else publish(run, Path(prepared.paths.worktree))
    )
    run.files.write("log", f"fault publication: {result}\n")


def publish(run: Run, workspace: Path) -> str:
    """Pin the exact commit to scan and publish on the run's own branch."""
    branch = f"agent/{run.files.name}"
    try:
        on_branch = worktree.git(workspace, "branch", "--show-current") == branch
        base = run.files.path("base").read_text().strip()
        if worktree.git(workspace, "status", "--porcelain", "--untracked-files=all"):
            if not on_branch:
                worktree.git(workspace, "switch", "--detach")
            worktree.git(workspace, "add", "-A")
            worktree.git(
                workspace,
                "-c", "user.name=Run launcher", "-c", "user.email=run-launcher",
                "commit", "--no-gpg-sign", "-m", "rescue: save failed run work",
            )  # fmt: skip
            if not on_branch:
                run.files.write("head", worktree.git(workspace, "rev-parse", "HEAD"))
        if not on_branch:
            return "kept local: checkout left the run branch"
        tip = push.head(workspace, branch)
    except FAILURES:
        return "kept local: could not save the run checkout"
    if tip == base:
        return "nothing to publish"
    run.files.write("head", tip)
    try:
        with push.publication(run.files.root, workspace, tip) as directory:
            return publish_commit(run, directory, branch, base, tip)
    except (*FAILURES, GhAppError):
        return "kept local: could not publish the run branch"


def publish_commit(run: Run, directory: Path, branch: str, base: str, tip: str) -> str:
    """Send the scanned object with a single, unforced refspec."""
    ref = f"refs/heads/{branch}"
    host, owner, name = repo.parts(run.request.repo)
    url = f"https://{host}/{owner}/{name}.git"
    account = next(
        c for c in run.settings.credentials if c.account == run.request.account
    )
    token = credentials.github_token(run.request, account)
    current = worktree.git(directory, "ls-remote", "--refs", url, ref, token=token)
    if current == f"{tip}\t{ref}":
        return "nothing to publish"
    if problem := scan(directory, base, tip):
        return f"kept local: {problem}"
    worktree.git(
        directory, "push", "--no-follow-tags", "--recurse-submodules=no", "--",
        url, f"{tip}:{ref}", token=token,
    )  # fmt: skip
    run.files.write("branch", branch)
    return "published"


def scan(directory: Path, base: str, tip: str) -> str | None:
    """Scan the outgoing range with policy from the run's starting commit."""
    try:
        config = write_policy(directory, base)
        options = ("--text", "--diff-merges=separate", f"{base}..{tip}")
        additions = push.history_git(
            directory, "log", "--format=", "--numstat", *options
        )
        done = subprocess.run(  # nosec B603 -- resolved tool and pinned history
            [
                host_tools.resolve("gitleaks"),
                "git",
                "--no-banner",
                "--no-color",
                "--redact",
                f"--exit-code={FINDING}",
                "--config",
                str(config),
                f"--log-opts={' '.join(options)}",
                str(directory),
            ],
            cwd=directory,
            env={
                "PATH": host_tools.search(),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=SCAN_SECONDS,
        )
    except FAILURES:
        return "secret scan unavailable"
    if done.returncode == FINDING:
        return "secret scan found a secret"
    # Gitleaks can exit zero when its Git subprocess fails.
    if done.returncode or re.search(r"\bERR\b", done.stderr):
        return "secret scan unavailable"
    if re.search(r"^[1-9][0-9]*\t", additions, re.MULTILINE) and not re.search(
        r"\b[1-9][0-9]* commits scanned\b", done.stderr
    ):
        return "secret scan unavailable"
    return None


def write_policy(directory: Path, base: str) -> Path:
    """Restore the committed policy and its base in the private scan directory."""
    name = secret_scan.GITLEAKS_EXTENDS
    policy = push.history_git(directory, "show", f"{base}:{name}")
    if push.history_git(directory, "ls-tree", base, "--", VENDORED_RULES):
        rules = push.history_git(directory, "show", f"{base}:{VENDORED_RULES}")
        (directory / "base.toml").write_text(rules)
        policy += '\n[extend]\npath = "base.toml"\n'
    else:
        policy = secret_scan.with_base(policy)
    config = directory / name
    config.write_text(policy)
    return config
