"""Refuse protected-branch pushes, unfetched rewrites, and invalid agent branches.

A non-fast-forward update to an unprotected branch is allowed only when the
remote tip git reports is the tip this clone fetched into
``refs/remotes/<remote>/<branch>``. Start-ref runs use
``refs/remotes/<remote>/fetched/heads/<branch>`` and pin the tip at launch.
The hook cannot see the flag, so a plain force reports the same tip. The
command-line guard (``guard_git_hooks``) refuses plain force, and this hook
logs ``old -> new`` on stderr for every rewrite it allows.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import sys
import tempfile
from functools import cached_property
from http.client import HTTPException
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev import userconfig
from tbhbdev.alpha.agent import settings
from tbhbdev.alpha.agent.decisions import host_names
from tbhbdev.alpha.agent.signing_parity import public_keys
from tbhbdev.alpha.gh_shim import RUN_NAME_ENV, agent_operation, session_root
from tbhbdev.alpha.identity.dispatcher import AGENT_EMAIL
from tbhbdev.alpha.repo import git, is_tbhb_dev_url

if TYPE_CHECKING:
    from collections.abc import Sequence

SAFE = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
)
HOOK_ARGUMENTS = 2
RUN_START_REF_ENV = "TBHBDEV_RUN_START_REF"
RUN_START_TIP_ENV = "TBHBDEV_RUN_START_TIP"
PROTECTED = frozenset({"refs/heads/main", "refs/heads/master"})
AGENT_BRANCH_PREFIX = "refs/heads/agent/"
AGENT_BRANCH_PREFIXES = (AGENT_BRANCH_PREFIX, "refs/heads/renovate/")
AGENT_KEY = Path(".config/tbhbdev/agent-signing-key.pub")
FINGERPRINT = re.compile(r"SHA256:\S+")
RANGE_LIMIT = 1000
# SHA-1 repositories write `gpgsig`, SHA-256 repositories `gpgsig-sha256`.
SIGNATURE_HEADER = re.compile(rb"gpgsig(-sha256)? ")


class InspectionError(ValueError):
    """Git could not supply data needed to check the push."""


def key_fingerprint(path: str, public_key: str | None = None) -> str:
    done = subprocess.run(  # nosec B603 B607
        ["ssh-keygen", "-l", "-f", path],
        input=public_key,
        capture_output=True,
        text=True,
        check=False,
    )
    found = FINGERPRINT.search(done.stdout)
    return found[0] if done.returncode == 0 and found else ""


def agent_fingerprint() -> str:
    """The fingerprint of this host's public signing key, or empty if unreadable."""
    return key_fingerprint(str(Path.home() / AGENT_KEY))


class SigningKeys:
    """Load signing keys on demand, once per push across all refs."""

    @cached_property
    def local(self) -> str:
        return agent_fingerprint()

    @cached_property
    def registered(self) -> set[str] | None:
        try:
            return {
                found
                for key in public_keys(timeout=5)
                if (found := key_fingerprint("-", key + "\n"))
            }
        except OSError, HTTPException, ValueError, KeyError, TypeError:
            return None

    def refusal(self, signer: str, ref: str, sha: str) -> str:
        """Explain why a signature was refused, or return empty when it passed."""
        if signer and signer == self.local:
            return ""
        registered = self.registered
        if signer in (registered or ()):
            return ""
        fallback = (
            " (could not fetch GitHub signing keys, checked the local key only)"
            if registered is None
            else ""
        )
        return (
            f"{ref}: agent commit {sha} lacks a valid agent signature{fallback}; "
            "push refused"
        )


def signature_fingerprint(raw: bytes) -> str:
    """The fingerprint of a commit's SSH signature, or empty if it has none."""
    head, _, _ = raw.partition(b"\n\n")
    lines = head.split(b"\n")
    start = next((i for i, x in enumerate(lines) if SIGNATURE_HEADER.match(x)), -1)
    if start < 0:
        return ""
    end = start + 1
    while end < len(lines) and lines[end].startswith(b" "):
        end += 1
    signature = b"\n".join(
        [
            SIGNATURE_HEADER.sub(b"", lines[start]),
            *(x[1:] for x in lines[start + 1 : end]),
        ]
    )
    rest = b"\n".join(lines[:start] + lines[end:]) + raw[len(head) :]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "signature"
        path.write_bytes(signature + b"\n")
        done = subprocess.run(  # nosec B603 B607
            ["ssh-keygen", "-Y", "check-novalidate", "-n", "git", "-s", str(path)],
            input=rest,
            capture_output=True,
            check=False,
        )
    found = FINGERPRINT.search((done.stdout + done.stderr).decode(errors="replace"))
    return found[0] if done.returncode == 0 and found else ""


def remote_tips() -> list[str]:
    """The tips of every remote-tracking ref and tag a pushed branch builds on."""
    code, out = git(
        *SAFE, "for-each-ref", "--format=%(objectname)", "refs/remotes", "refs/tags"
    )
    if code:
        message = "git for-each-ref could not list remote-tracking refs and tags"
        raise InspectionError(message)
    return out.split()


def known_objects(shas: list[str]) -> list[str]:
    """The shas this clone holds, from one ``cat-file --batch-check`` call."""
    done = subprocess.run(  # nosec B603 B607
        ["git", *SAFE, "cat-file", "--batch-check=%(objectname)"],
        input="".join(f"{sha}\n" for sha in shas),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        message = "git cat-file --batch-check could not inspect the push's objects"
        raise InspectionError(message)
    return [row for row in done.stdout.splitlines() if not row.endswith(" missing")]


def pushed_commits(local: str, exclude: list[str]) -> tuple[int, list[str]]:
    """The commits reachable from ``local`` but not from ``exclude``, capped."""
    done = subprocess.run(  # nosec B603 B607
        ["git", *SAFE, "rev-list", f"--max-count={RANGE_LIMIT + 1}", "--stdin"],
        input="\n".join([local, "--not", *exclude]) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout.split()


def commit_bytes(sha: str) -> bytes:
    """The commit object as git stores it, byte for byte."""
    done = subprocess.run(  # nosec B603 B607
        ["git", *SAFE, "cat-file", "commit", sha],
        capture_output=True,
        check=False,
    )
    if done.returncode or not done.stdout:
        message = f"git cat-file could not read commit {sha}"
        raise InspectionError(message)
    return done.stdout


def agent_commit_problems(
    ref: str, local: str, exclude: list[str], keys: SigningKeys
) -> list[str]:
    """Check signatures for newly pushed agent commits.

    ``exclude`` holds every remote-tracking and tag tip and the old sha git reports. A
    failed listing or a range over RANGE_LIMIT refuses the push.
    """
    code, shas = pushed_commits(local, exclude)
    if code:
        return [f"{ref}: cannot list the commits to push; push refused"]
    if len(shas) > RANGE_LIMIT:
        return [f"{ref}: the push adds over {RANGE_LIMIT} commits; push refused"]
    problems: list[str] = []
    for sha in shas:
        raw = commit_bytes(sha)
        headers = raw.partition(b"\n\n")[0].split(b"\n")
        idents = [
            next((line for line in headers if line.startswith(role)), b"")
            for role in (b"author ", b"committer ")
        ]
        if not any(f"<{AGENT_EMAIL}>".encode() in ident for ident in idents):
            continue
        if problem := keys.refusal(signature_fingerprint(raw), ref, sha):
            problems.append(problem)
    return problems


def fetched_tip(remote: str, ref: str, start_ref: str | None) -> str:
    """The tip of the remote-tracking branch for a pushed ref, or empty."""
    if not ref.startswith("refs/heads/"):
        return ""
    branch = ref.removeprefix("refs/heads/")
    tracking = f"fetched/heads/{branch}" if ref == start_ref else branch
    code, out = git(
        *SAFE,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/{remote}/{tracking}^{{commit}}",
    )
    return "" if code else out


def destination_problem(
    ref: str, *, deleting: bool, agent_push: bool, run_name: str, start_ref: str | None
) -> str:
    """The refusal for a disallowed push destination, or empty when allowed."""
    if run_name and ref.startswith("refs/heads/"):
        branch = start_ref or f"refs/heads/agent/{run_name}"
        if ref != branch:
            return f"{ref}: this run may only push {branch}"
    if ref in PROTECTED:
        message = (
            "push to a protected branch refused; "
            "push a new branch and open a pull request"
        )
        return f"{ref}: {message}"
    if ref == start_ref and deleting:
        return f"{ref}: deletion refused; this run may only update its start ref"
    if agent_push and ref.startswith("refs/heads/") and ref != start_ref:
        if deleting and not ref.startswith(AGENT_BRANCH_PREFIX):
            return f"{ref}: deletion refused; agents may delete only agent/ branches"
        if not ref.startswith(AGENT_BRANCH_PREFIXES):
            return f"{ref}: push refused; push to an agent/<name> branch instead"
    return ""


def ref_problems(
    remote: str,
    row: tuple[str, str, str],
    tips: Sequence[str],
    keys: SigningKeys,
    start_ref: str | None,
) -> list[str]:
    """The refusals for one pushed ref: LOCAL, REF, and the remote's OLD value."""
    local, ref, old = row
    deleting = not local.strip("0")
    if deleting:
        return []
    if ref == start_ref:
        if not old.strip("0"):
            return [f"{ref}: the start ref must already exist; push refused"]
        if old != os.environ.get(RUN_START_TIP_ENV):
            return [f"{ref}: remote tip changed since fetch; push refused"]
    exclude = known_objects([*tips, old] if old.strip("0") else list(tips))
    problems = agent_commit_problems(ref, local, exclude, keys)
    ancestry = (
        git(*SAFE, "merge-base", "--is-ancestor", old, local)[0]
        if old.strip("0")
        else 0
    )
    if ancestry:
        if fetched_tip(remote, ref, start_ref) != old:
            problems.append(
                f"{ref}: non-fast-forward push refused; the remote tip is not "
                "the fetched one, so fetch and push with --force-with-lease"
            )
        elif ancestry == 1:
            print(f"pre-push-guard: {ref} rewritten {old} -> {local}", file=sys.stderr)
        else:
            problems.append(
                f"{ref}: git merge-base could not check ancestry; push refused"
            )
    return problems


def push_problems(
    remote: str, lines: Sequence[str], *, agent_push: bool, run_name: str
) -> list[str]:
    rows: list[tuple[str, str, str]] = []
    known_hosts: set[str] | None = None
    for line in lines:
        _, local, ref, old = line.split()  # ValueError: main() refuses the push
        if ref.startswith("refs/heads/") and local.strip("0") and not old.strip("0"):
            if known_hosts is None:
                known_hosts = settings.known_host_names()
            if host_names.contains_host_name(
                ref.removeprefix("refs/heads/"), known_hosts
            ):
                return ["push refused because the branch name contains a host name"]
        rows.append((local, ref, old))
    tips = remote_tips()
    keys = SigningKeys()
    problems = []
    start_ref = os.environ.get(RUN_START_REF_ENV) if run_name else None
    for row in rows:
        local, ref, _ = row
        problem = destination_problem(
            ref,
            deleting=not local.strip("0"),
            agent_push=agent_push,
            run_name=run_name,
            start_ref=start_ref,
        )
        if problem:
            problems.append(problem)
        else:
            problems.extend(ref_problems(remote, row, tips, keys, start_ref))
    return problems


def inspect_push(remote: str, target: str, lines: Sequence[str]) -> list[str]:
    """Require managed identity for agent pushes to the org."""
    if not target.strip():
        message = "cannot determine the push destination"
        raise InspectionError(message)
    run_name = os.environ.get(RUN_NAME_ENV, "")
    agent_org_push = is_tbhb_dev_url(target) and agent_operation(os.environ)
    if agent_org_push and not run_name.strip():
        message = (
            f"{RUN_NAME_ENV} is missing; push from a managed agent run "
            "with its hooks installed (install clone hooks with "
            "`tbhbdev alpha install`)"
        )
        raise InspectionError(message)
    agent_push = agent_org_push or session_root(os.environ) is not None
    return push_problems(remote, lines, agent_push=agent_push, run_name=run_name)


def main(argv: list[str], prog: str) -> int:
    if len(argv) != HOOK_ARGUMENTS:
        print(f"usage: {prog} REMOTE URL", file=sys.stderr)
        return 1
    for variable in ("SKIP", "PREK_SKIP"):
        if os.environ.get(variable):
            print(f"pre-push-guard: {variable} is set; push refused", file=sys.stderr)
            return 1
    try:
        problems = inspect_push(argv[0], argv[1], sys.stdin.read().splitlines())
    except (InspectionError, OSError, userconfig.ConfigError) as exc:
        problems = [f"pre-push-guard: {exc}; push refused"]
    except ValueError:
        problems = ["pre-push-guard: cannot inspect push; push refused"]
    for problem in problems:
        print(problem, file=sys.stderr)
    return int(bool(problems))
