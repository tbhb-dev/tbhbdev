"""Save and publish a failed build's work from the launcher."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import secret_scan
from tbhbdev.alpha.agent import credentials, host_tools, push
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Profile
from tbhbdev.alpha.gh_app import GhAppError

if TYPE_CHECKING:
    from collections.abc import Generator

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
    ref = f"refs/heads/{branch}"
    try:
        base = run.files.path("base").read_text().strip()
        refs, checkout = push.local_refs(workspace)
        parent = refs.get("HEAD", refs.get("detached HEAD"))
        if parent is None:
            return "kept local: could not save the run checkout"
        on_branch = checkout == f"ref: {ref}"
        with push.publication(run.files.root, workspace, parent) as directory:
            saved = save_checkout(directory, workspace, ref if on_branch else "HEAD")
        if not on_branch:
            if saved is not None:
                run.files.write("head", saved)
            return "kept local: checkout left the run branch"
        tip = saved or parent
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


def save_checkout(directory: Path, checkout: Path, ref: str) -> str | None:
    """Stage with private Git metadata and save the snapshot under a ref lock."""
    metadata = checkout / ".git"
    index = metadata / "index"
    pack = metadata / "objects" / "pack"
    destination = metadata / ref
    if any(path.resolve() != path for path in (index, pack, destination.parent)):
        message = "unexpected symlink in run Git metadata"
        raise OSError(message)
    private_index = directory / "index"
    with suppress(FileNotFoundError):
        # Git uses the index timestamp to detect same-tick worktree edits.
        shutil.copy2(index, private_index)
    git = ("--work-tree", str(checkout))
    status = push.history_git(
        directory,
        "--no-optional-locks",
        *git,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=dirty",
    )
    if not status:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with replace_locked(destination) as ref_lock, replace_locked(index) as index_lock:
        private_index.unlink(missing_ok=True)
        with suppress(FileNotFoundError):
            shutil.copy2(index, private_index)
        tip = commit_checkout(directory, git, pack)
        shutil.copy2(private_index, index_lock)
        ref_lock.write_text(tip + "\n")
    return tip


@contextmanager
def replace_locked(target: Path) -> Generator[Path]:
    """Install a file through the same exclusive lock used by Git."""
    lock = target.with_name(target.name + ".lock")
    lock.touch(exist_ok=False)
    try:
        yield lock
        lock.replace(target)
    except BaseException:
        lock.unlink(missing_ok=True)
        raise


def commit_checkout(directory: Path, git: tuple[str, ...], pack: Path) -> str:
    """Create the commit privately, then install its objects in the run clone."""
    push.history_git(directory, *git, "add", "-A")
    push.history_git(
        directory,
        "-c",
        "user.name=Run launcher",
        "-c",
        "user.email=run-launcher",
        *git,
        "commit",
        "--no-gpg-sign",
        "-m",
        "rescue: save failed run work",
    )
    commit = push.history_git(directory, "rev-parse", "HEAD").strip()
    push.history_git(
        directory,
        "pack-objects",
        "--revs",
        str(pack / "pack"),
        revisions=f"{commit}\n^{commit}^\n",
    )
    return commit


def publish_commit(run: Run, directory: Path, branch: str, base: str, tip: str) -> str:
    """Send the scanned object with a single, unforced refspec."""
    ref = f"refs/heads/{branch}"
    host, owner, name = repo.parts(run.request.repo)
    url = f"https://{host}/{owner}/{name}.git"
    account = next(
        c for c in run.settings.credentials if c.account == run.request.account
    )
    token = credentials.github_token(run.request, account)
    current = push.remote_git(directory, "ls-remote", "--refs", url, ref, token=token)
    if current == f"{tip}\t{ref}":
        return "nothing to publish"
    if problem := scan(directory, base, tip):
        return f"kept local: {problem}"
    push.remote_git(
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
