"""Open a draft for published work from a failed build."""

from __future__ import annotations

import os
import re
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2

from tbhbdev import github_tls
from tbhbdev.alpha import opened, secret_scan, size_gate
from tbhbdev.alpha.agent import credentials, host_tools, push, runfiles
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions.runfiles_names import PR
from tbhbdev.alpha.agent.run_host.publication_plan import branch_name
from tbhbdev.alpha.identity.commit import grammar
from tbhbdev.alpha.pull_request import attribution, publish, sizing
from tbhbdev.alpha.pull_request.description import trailer_lines
from tbhbdev.alpha.pull_request.draft import Checked
from tbhbdev.alpha.pull_request.metadata import remote_head

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tbhbdev.alpha.agent.run_host.state import Run

SAVE = "rescue: save failed run work"
TIME_LIMIT = 124
IO_SECONDS = 5


def title(subjects: Sequence[str]) -> str:
    """Use the earliest work commit, or the automatic save if it stands alone."""
    return next((subject for subject in subjects if subject != SAVE), SAVE)[:100]


def body(rc: int, checks: Sequence[str], *, saved: bool) -> str:
    """Describe the fault, checks and review status in that order."""
    fault = "hit the time limit" if rc == TIME_LIMIT else f"ended with code {rc}"
    checked = (
        f"Recorded passing pre-push checks were {', '.join(checks)}."
        if checks
        else "There is no record of a pre-push check."
    )
    save = "The last commit is an automatic save of unfinished work. " if saved else ""
    return f"The run {fault}.\n\n{checked}\n\n{save}The branch has not passed review."


def open_draft(run: Run, workspace: Path, rc: int, publication: str) -> None:
    """Try once after publication. The fault's recorded exit code is final."""
    if publication not in ("published", "nothing to publish"):
        return
    result = "not opened: draft attempt failed"
    with suppress(Exception, SystemExit):
        try:
            if not run.files.path("head").exists():
                return
            tip = run.files.path("head").read_text().strip()
            base = run.files.path("base").read_text().strip()
            if base == tip:
                return
            result = _attempt(run, workspace, rc, base, tip)
        except secret_scan.FindingRefusal:
            result = "not opened: gitleaks finds a secret in the PR title or body"
    if result:
        with suppress(Exception):
            run.files.write("log", f"draft pull request: {result}\n")


def _attempt(run: Run, workspace: Path, rc: int, base: str, tip: str) -> str:
    branch = branch_name(run.files.name, run.request.start_ref)
    host, owner, name = repo.parts(run.request.repo)
    target = f"{owner}/{name}"
    if host != repo.DEFAULT_GIT_HOST:
        return "not opened: unsupported repository"
    endpoint = f"https://api.github.com/repos/{target}/pulls"
    account = next(
        c for c in run.settings.credentials if c.account == run.request.account
    )
    token = credentials.github_token(run.request, account)
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with (
        httpx2.Client(
            headers=headers,
            timeout=IO_SECONDS,
            trust_env=False,
            verify=github_tls.public_roots(),
        ) as client,
        push.publication(run.files.root, workspace, tip) as directory,
    ):
        # Match PR 227's lookup, including PRs that have already closed.
        params = {"head": f"{owner}:{branch}", "state": "all", "per_page": 1}
        pulls = None
        with suppress(Exception):
            pulls = client.get(endpoint, params=params).raise_for_status().json()
        if not isinstance(pulls, list) or (not pulls and not token):
            return f"not opened: cannot check {branch}"
        if pulls:
            return ""
        url = f"https://github.com/{target}.git"
        remote = partial(push.remote_git, directory, token=token)
        ref = f"refs/heads/{branch}"
        listing = remote("ls-remote", "--symref", url, "HEAD", ref)
        base_branch = remote_head(listing)
        default_tip = re.search(r"^([0-9a-f]{40})\tHEAD$", listing, re.MULTILINE)
        on_origin = f"{tip}\t{ref}" in listing.splitlines()
        if not (base_branch and default_tip and on_origin):
            return "not opened: cannot verify the published tip and base branch"
        git = partial(push.history_git, directory)
        remote("fetch", "--no-tags", "--no-recurse-submodules", url, default_tip[1])
        if problem := _size_problem(directory, default_tip[1], tip, branch):
            return f"not opened: {problem}"
        records = git("log", "--reverse", "--format=%H %T %s", f"{base}..{tip}")
        subjects, checks = _history(workspace, records)
        subject = title(subjects)
        text = body(rc, checks, saved=SAVE in subjects)
        fmt = "--format=%(trailers:only,unfold)"
        found = trailer_lines(
            git("log", "--reverse", fmt, f"{default_tip[1]}..{tip}"), grammar.LINE
        )
        root, opener = attribution.opened_by(os.environ)
        text = attribution.description(
            Checked([], subject, [subject, text]), os.environ, found, opener
        )
        _scan(directory, base, subject, text)

        payload = {"title": subject, "head": branch, "draft": True}
        payload |= {"base": base_branch, "body": text}
        reply = client.post(endpoint, json=payload)
    return _record(run, reply, target, root)


def _record(run: Run, reply: httpx2.Response, target: str, root: str | None) -> str:
    number = reply.raise_for_status().json().get("number")
    if type(number) is not int or number < 1:
        return "not opened: PR creation returned no number"
    with suppress(Exception):
        run.files.write(PR.removeprefix("."), f"{number}\n")
        if root is not None:
            opened.record_opened(root, [f"{target}#{number}"])
        return f"opened {number}"
    return f"opened {number} (recording failed)"


def _history(workspace: Path, records: str) -> tuple[list[str], list[str]]:
    """Collect subjects and passing stages recorded for these commit trees."""
    subjects: list[str] = []
    checks: set[str] = set()
    for line in records.split("\n")[:-1]:
        commit, tree, subject = line.split(" ", 2)
        subjects.append(subject)
        path = workspace / ".git/tbhbdev-preflight" / f"{commit}.stages"
        with suppress(OSError, ValueError):
            record = runfiles.read_owned(path, 16 * 1024, "preflight stages")
            if record.startswith(f"tree {tree}\n"):  # Match PR 225's stage records.
                checks.update(re.findall(r"^([a-z0-9-]+) PASS$", record, re.MULTILINE))
    return subjects, sorted(checks)


def _scan(directory: Path, base: str, subject: str, text: str) -> None:
    """Use the starting tree's policy and the full scan for unknown visibility."""
    _copy_policy(directory, base, secret_scan.GITLEAKS_EXTENDS)
    gitleaks = host_tools.resolve("gitleaks")
    scanner = partial(secret_scan.scan, root=directory, gitleaks=gitleaks)
    publish.post_text(subject, text, "", scan=scanner)


def _size_problem(directory: Path, base: str, tip: str, branch: str) -> str | None:
    """Run the common counter with protected Git metadata and the pinned tree."""
    _copy_policy(directory, tip, ".github/pr-size-exempt.txt")
    search = host_tools.search()
    counter_path = ":".join((search, *host_tools.account_path(str(Path.home()))))
    env = {
        "PATH": search,
        "TMPDIR": str(directory),
        "GIT_DIR": str(directory),
        "GIT_WORK_TREE": str(directory),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_SOURCE": tip,
    }
    counts = size_gate.size_counts(
        base,
        tip,
        head_branch=branch,
        scc=host_tools.resolve("scc", counter_path),
        git=host_tools.resolve("git"),
        env=env,
    )
    return sizing.size_excess(counts) if counts.failed else None


def _copy_policy(directory: Path, commit: str, name: str) -> None:
    git = partial(push.history_git, directory)
    if git("ls-tree", "--name-only", commit, "--", name):
        path = directory / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(git("show", f"{commit}:{name}"))
