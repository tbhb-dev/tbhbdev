"""Verify a required push and publish successful review results."""

from __future__ import annotations

import httpx2

from tbhbdev import github_tls
from tbhbdev.alpha.agent import credentials, review_post
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.run_host import publication_plan, republish
from tbhbdev.alpha.agent.run_host.state import Prepared, Run, RunInterruptedError

NO_COMMIT = 6


NO_PUSH = 9


NO_REVIEW = 10


NO_PUSH_MOVED = 12


def post_review(run: Run, prepared: Prepared) -> int:
    if run.files.uncertain:
        run.files.write("log", "review not posted: harness shutdown unconfirmed\n")
        return NO_REVIEW
    try:
        return 0 if review_post.posted(run, prepared) else NO_REVIEW
    except RunInterruptedError:
        # A signal during posting follows the run interrupt path and records 143.
        raise
    except Exception as exc:  # noqa: BLE001 -- record posting failures safely.
        run.files.write("log", f"review not posted: {type(exc).__name__}\n")
        return NO_REVIEW


def github_get(run: Run, endpoint: str) -> httpx2.Response:
    _, owner, repository = repo.parts(run.request.repo)
    url = f"https://api.github.com/repos/{owner}/{repository}/{endpoint}"
    headers = {"Accept": "application/vnd.github+json"}
    if (token := credentials.fetch_token(run.request, run.settings)) is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx2.get(
        url, headers=headers, timeout=20, verify=github_tls.public_roots()
    )


def origin_tip(run: Run) -> str | None:
    """Read the exact branch from origin. Empty means absent, None unreadable."""
    branch = publication_plan.branch_name(run.files.name, run.request.start_ref)
    try:
        response = github_get(run, f"git/ref/heads/{branch}")
        if response.status_code == httpx2.codes.NOT_FOUND:
            return ""
        response.raise_for_status()
        record = response.json()
    except OSError, ValueError, httpx2.HTTPError:
        return None
    return publication_plan.ref_tip(record, f"refs/heads/{branch}")


def origin_contains(run: Run, commit: str) -> bool | None:
    tip = origin_tip(run)
    if tip is None or not tip or tip == commit:
        return None if tip is None else bool(tip)
    # Compare pinned commits so that a concurrent push cannot change the answer.
    try:
        response = github_get(run, f"compare/{commit}...{tip}?per_page=1")
        if response.status_code == httpx2.codes.NOT_FOUND:
            return False
        response.raise_for_status()
        record = response.json()
    except OSError, ValueError, httpx2.HTTPError:
        return None
    return publication_plan.origin_ahead(record)


def require_commit(run: Run, prepared: Prepared, rc: int) -> int:
    if rc or not run.request.expect_commit:
        return rc
    if not (head := run.files.path("head")).exists():
        run.files.write(
            "log", "no new commit: the build required one (--expect-commit)\n"
        )
        return NO_COMMIT
    published = origin_contains(run, head.read_text().strip())
    branch = publication_plan.branch_name(run.files.name, run.request.start_ref)
    if published:
        run.files.write("branch", branch)
        return 0
    if (
        published is False
        and run.request.start_ref is not None
        and (status := republish.republish(run, prepared)) is not None
    ):
        return status
    reason = "cannot verify" if published is None else "build commit missing from"
    run.files.write("log", f"push failed: {reason} {branch} on origin\n")
    return NO_PUSH
