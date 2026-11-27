"""Save and publish a failed build's work from the launcher."""

from __future__ import annotations

import re
import subprocess  # nosec B404
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import secret_scan
from tbhbdev.alpha.agent import credentials, host_tools, push
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
    ref = f"refs/heads/{branch}"
    failure = "could not save the run checkout"
    try:
        base = run.files.path("base").read_text().strip()
        refs, checkout = push.local_refs(workspace)
        parent = refs.get("HEAD", refs.get("detached HEAD", ""))
        on_branch = checkout == f"ref: {ref}"
        with push.publication(run.files.root, workspace, parent) as directory:
            saved = save_checkout(directory, workspace, ref if on_branch else "HEAD")
            tip = saved or parent
            if saved or (on_branch and tip != base):
                run.files.write("head", tip)
            if not on_branch:
                return "kept local: checkout left the run branch"
            if tip == base:
                return "nothing to publish"
            failure = "could not publish the run branch"
            return publish_commit(run, directory, branch, base, tip)
    except (*FAILURES, GhAppError):
        return f"kept local: {failure}"


def save_checkout(directory: Path, checkout: Path, ref: str) -> str | None:
    """Stage with private Git metadata and save the snapshot under a ref lock."""
    index = checkout / ".git/index"
    objects = checkout / ".git/objects"
    destination = checkout / ".git" / ref
    paths = (index, objects, destination.parent, *objects.iterdir())
    if any(path.resolve() != path for path in paths):
        message = "unexpected symlink in run Git metadata"
        raise OSError(message)
    with (directory / "config").open("a") as config:
        config.write(
            "[core]\nfsmonitor = false\n"
            "[user]\nname = Run launcher\nemail = run-launcher\n"
        )
    location = ("--git-dir", str(directory), "--work-tree", str(checkout))
    env = {"GIT_INDEX_FILE": str(index), "GIT_OBJECT_DIRECTORY": str(objects)}
    git = partial(push.history_git, directory, *location, extra_env=env)
    status = ("status", "--porcelain", "-uall", "--ignore-submodules=dirty")
    if not git("--no-optional-locks", *status):
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = destination.with_name(destination.name + ".lock")
    stream = lock.open("x")
    try:
        with stream:
            git("add", "-A")
            git("commit", "--no-gpg-sign", "-m", "rescue: save failed run work")
            tip = git("rev-parse", "HEAD").strip()
            stream.write(tip + "\n")
        lock.replace(destination)
    except BaseException:
        lock.unlink(missing_ok=True)
        raise
    return tip


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
        git_log = f"--log-opts={' '.join(options)}"
        flags = ("git", "--no-banner", "--no-color", "--redact")
        args = (*flags, f"--exit-code={FINDING}", "--config", str(config), git_log)
        done = subprocess.run(  # nosec B603 -- resolved tool and pinned history
            [host_tools.resolve("gitleaks"), *args, str(directory)],
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
