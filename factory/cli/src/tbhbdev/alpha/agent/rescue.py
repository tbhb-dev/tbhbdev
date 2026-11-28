"""Save and publish a failed build's work from the launcher."""

from __future__ import annotations

import re
import subprocess  # nosec B404
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import pre_push_guard, secret_scan
from tbhbdev.alpha.agent import credentials, host_tools, push
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid, Profile
from tbhbdev.alpha.gh_app_runtime import GhAppError

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import Prepared, Run

FAILURES = (OSError, ValueError, subprocess.SubprocessError)
FINDING = 3
SCAN_SECONDS = 60
VENDORED_RULES = "lib/rules/lint/gitleaks/base.toml"


def on_fault(run: Run, prepared: Prepared | None, rc: int) -> None:
    """Save failed build work and log the publication result."""
    if rc == 0 or prepared is None or run.request.profile is not Profile.BUILD:
        return
    if not all(run.files.path(name).exists() for name in ("launched", "base")):
        return
    result = "kept local: harness shutdown unconfirmed"
    if not run.files.uncertain:
        result = publish(run, Path(prepared.paths.worktree))
    run.files.write("log", f"fault publication: {result}\n")


def publish(run: Run, workspace: Path) -> str:
    """Pin the exact commit to scan and publish to the run's destination."""
    ref = f"refs/heads/agent/{run.files.name}"
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
            return publish_commit(run, directory, ref, base, tip)
    except Invalid as exc:
        return f"kept local: {exc}"
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


def publish_commit(run: Run, directory: Path, ref: str, base: str, tip: str) -> str:
    """Send the scanned object with a single, unforced refspec."""
    request = run.request
    start_ref = request.start_ref
    remote_ref = start_ref or ref
    if problem := pre_push_guard.destination_problem(
        remote_ref,
        deleting=False,
        agent_push=True,
        run_name=run.files.name,
        start_ref=start_ref,
    ):
        return f"kept local: {problem}"
    host, owner, name = repo.parts(request.repo)
    url = f"https://{host}/{owner}/{name}.git"
    account = next(c for c in run.settings.credentials if c.account == request.account)
    token = credentials.fresh_token(request, account)
    git = partial(push.remote_git, directory, token=token)
    current = git("ls-remote", "--refs", url, remote_ref)
    if start_ref and current != f"{base}\t{start_ref}":
        return "kept local: start ref moved"
    if current == f"{tip}\t{remote_ref}":
        return "nothing to publish"
    if problem := scan(directory, base, tip):
        return f"kept local: {problem}"
    if pre_push_guard.agent_commit_problems(
        remote_ref, tip, [base], pre_push_guard.SigningKeys(), directory=directory
    ):
        return "kept local: agent commit signature refused"
    refspec = f"{tip}:{remote_ref}"
    git("push", "--no-follow-tags", "--recurse-submodules=no", "--", url, refspec)
    run.files.write("branch", remote_ref.removeprefix("refs/heads/"))
    return "published"


def scan(directory: Path, base: str, tip: str) -> str | None:
    """Scan the outgoing range with the host policy and the committed rules."""
    try:
        config = write_policy(directory, base)
        diff = ("--text", "--diff-merges=separate", f"{base}..{tip}")
        stats = push.history_git(directory, "log", "--format=", "--numstat", *diff)
        flags = ("--no-banner", "--no-color", "--redact", f"--exit-code={FINDING}")
        args = (*flags, "--config", str(config), f"--log-opts={' '.join(diff)}")
        done = subprocess.run(  # nosec B603 -- resolved tool and pinned history
            [host_tools.resolve("gitleaks"), "git", *args, str(directory)],
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
    added_lines = re.search(r"(?m)^[1-9][0-9]*\t", stats)
    scanned = re.search(r"\b[1-9][0-9]* commits scanned\b", done.stderr)
    return "secret scan unavailable" if added_lines and not scanned else None


def write_policy(directory: Path, base: str) -> Path:
    """Extend the run's starting policy with the host's public scan rules."""
    name = secret_scan.GITLEAKS_EXTENDS
    private = secret_scan.private_config().read_text()
    reduced = secret_scan.without_name_rules(private, secret_scan.REPO_RULES)
    private, paths = secret_scan.EXTENDS_PATH.subn(rf'\g<1>"{name}"', reduced or "")
    if not paths or not secret_scan.CANARY.search(private):
        message = "invalid private gitleaks config"
        raise ValueError(message)
    policy = push.history_git(directory, "show", f"{base}:{name}")
    if push.history_git(directory, "ls-tree", base, "--", VENDORED_RULES):
        rules = push.history_git(directory, "show", f"{base}:{VENDORED_RULES}")
        (directory / "base.toml").write_text(rules)
        policy += '\n[extend]\npath = "base.toml"\n'
    else:
        policy = secret_scan.with_base(policy)
    (directory / name).write_text(policy)
    config = directory / "private.toml"
    config.write_text(private)
    return config
