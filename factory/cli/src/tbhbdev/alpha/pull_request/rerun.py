# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Rerun failed or stuck workflow jobs for the current pull request head."""

import datetime as dt
import sys
import time
from typing import cast

from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.metadata import pr_view_args
from tbhbdev.alpha.pull_request.run_results import failures, in_progress, job_is_stale
from tbhbdev.alpha.pull_request.tools import GH

CANCEL_POLLS = 12
CANCEL_PAUSE = 5.0


def utcnow() -> dt.datetime:
    """The current time in UTC."""
    return dt.datetime.now(dt.UTC)


def pause(seconds: float) -> None:
    """Sleep between polls of a cancelled run."""
    time.sleep(seconds)


def run_view(run_id: object, field: str) -> dict[str, object]:
    """A run's ``gh run view --json FIELD`` mapping.

    Raises:
        SystemExit: gh can't read the run.
    """
    done = pr_tools.run(
        [pr_tools.find_tool(GH), "run", "view", str(run_id), "--json", field]
    )
    data = pr_github.gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read run {run_id}: {why}"
        raise SystemExit(message)
    return cast("dict[str, object]", data)  # pragma: no mutate


def stuck_job(run_id: object) -> bool:
    """Whether a job of the run has been in progress for over an hour.

    Raises:
        SystemExit: gh can't read the run.
    """
    jobs = cast(
        "list[dict[str, str]]", run_view(run_id, "jobs")["jobs"]
    )  # pragma: no mutate
    return any(
        job["status"] == "in_progress" and job_is_stale(job["startedAt"], utcnow())
        for job in jobs
    )


def finished_as(run_id: object) -> str | None:
    """The run's conclusion once it completes within the bounded polls, else None."""
    for _ in range(CANCEL_POLLS):
        view = run_view(run_id, "status,conclusion")
        if view["status"] == "completed":
            return str(view["conclusion"])
        pause(CANCEL_PAUSE)
    return None


def run_numbers(run_id: object) -> list[object]:
    """The pull request numbers GitHub lists for the run."""
    return cast(
        "list[object]",
        pr_github.gh_json(
            [
                "api",
                f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}",
                "--jq",
                "[.pull_requests[].number]",
            ]
        ),
    )  # pragma: no mutate


def sole_pull_request(run_id: object, number: int) -> bool:
    """Whether the run belongs to the pull request alone.

    A run that lists other pull requests too is theirs as much as ours, so
    cancelling it would hit them; the note names them.
    """
    numbers = run_numbers(run_id)
    if number in numbers and numbers != [number]:
        others = ", ".join(str(n) for n in numbers if n != number)
        print(f"run {run_id} also belongs to PR {others}; skipping", file=sys.stderr)
    return numbers == [number]


def cancel_and_rerun(run_id: str) -> int:
    """Cancel the run, wait until it completes as cancelled, then rerun it."""
    code = pr_github.invoke_gh([GH, "run", "cancel", run_id], "")[0]
    if code:
        return code
    outcome = finished_as(run_id)
    if outcome is None:
        print(f"run {run_id} did not finish cancelling; no rerun", file=sys.stderr)
        return 1
    if outcome != "cancelled":
        print(f"run {run_id} completed as {outcome}; no rerun", file=sys.stderr)
        return 1
    return pr_github.invoke_gh([GH, "run", "rerun", run_id], "")[0]


def rerun_stuck(number: int) -> int:
    """Cancel PR N's stuck current-head run, wait for the cancel, then rerun it.

    Only a ``pull_request`` run on the PR's head commit and head branch
    counts. A fork's PR is refused, because its branch name can collide.

    Raises:
        SystemExit: gh can't read the pull request or its runs.
    """
    done = pr_tools.run(
        [
            pr_tools.find_tool(GH),
            *pr_view_args(number, "headRefOid,headRefName,isCrossRepository"),
        ]
    )
    data = pr_github.gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read PR {number}: {why}"
        raise SystemExit(message)
    meta = cast("dict[str, object]", data)  # pragma: no mutate
    if meta["isCrossRepository"]:
        print(f"PR {number} comes from a fork; rerun --stuck refuses", file=sys.stderr)
        return 1
    head = str(meta["headRefOid"])
    runs = pr_github.gh_json(
        [
            "run",
            "list",
            "--commit",
            head,
            "--event",
            "pull_request",
            "--branch",
            str(meta["headRefName"]),
            "--limit",
            "20",
            "--json",
            "databaseId,status,headSha",
        ]
    )
    stuck = [
        item
        for item in in_progress(runs, head)
        if sole_pull_request(item["databaseId"], number)
        and stuck_job(item["databaseId"])
    ]
    if not stuck:
        print(f"no stuck run for the current head of PR {number}", file=sys.stderr)
        return 1
    if pr_github.head_of(number) != head:
        print(f"PR {number} moved to a new head; run again", file=sys.stderr)
        return 1
    return cancel_and_rerun(str(stuck[0]["databaseId"]))


def rerun(number: int) -> int:
    """Rerun the failed jobs of every failed run for PR N's current head.

    Raises:
        SystemExit: gh can't read the pull request or its runs.
    """
    head = pr_github.head_of(number)
    pages = pr_github.gh_json(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={head}&per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    failed = failures(pages)
    if not failed:
        print(f"no failed run for the current head of PR {number}", file=sys.stderr)
        return 1
    code = 0
    for item in failed:
        result = pr_github.invoke_gh(
            [GH, "run", "rerun", str(item["id"]), "--failed"], ""
        )[0]
        code = code or result
    return code
