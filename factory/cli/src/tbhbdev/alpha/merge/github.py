# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read the pull request, its repository rules and its merge queue from GitHub."""

from urllib.parse import quote

from tbhbdev.alpha.identity.dispatcher import (
    AGENT_EMAIL,
    ATTR_PAIR,
    HASH_RE,
    SLUGS,
    llm_trailers,
)
from tbhbdev.alpha.merge.evidence import (
    EvidenceError,
    PullRequest,
    approval_reasons,
    check_entry,
    commit_messages,
    parse_pull_request,
    refusals,
    review_snapshot,
    view_entries,
)
from tbhbdev.alpha.merge.queue import parse_queue_state
from tbhbdev.alpha.merge.review_gate import Attribution, Pull, Review, evaluate
from tbhbdev.alpha.merge.rules import (
    MERGE_FIELD,
    parse_branch_rules,
    require_merge_commits,
    required_checks,
)
from tbhbdev.alpha.pull_request import GH, NAME_WITH_OWNER, find_tool, run, target_of_pr

VIEW_FIELDS = (
    "title,body,headRefOid,baseRefName,state,isDraft,mergeable,"
    "mergeStateStatus,statusCheckRollup,author,url,commits,reviewDecision"
)
REVIEWS_APP = "tbhb-dev-agent-reviews"
REVIEWS_QUERY = """\
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      headRefOid reviewDecision
      reviews(first: 100) {
        pageInfo { hasNextPage }
        nodes { id state body commit { oid } author { login __typename } }
      }
    }
  }
}
"""
QUEUE_QUERY = """\
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      state
      isInMergeQueue
      mergeCommit { oid }
      timelineItems(last: 1, itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) {
        nodes { ... on RemovedFromMergeQueueEvent { id reason } }
      }
    }
  }
}
"""


ATTRIBUTION = Attribution(
    llm_trailers,
    ATTR_PAIR,
    frozenset({*SLUGS.values(), "claude", "agy", "gemini"}),
    HASH_RE,
)


def fetch(number: int) -> PullRequest:
    """Read the PR through gh.

    Raises:
        SystemExit: gh fails or does not return JSON.
    """
    argv = [find_tool(GH), "pr", "view", str(number), "--json", VIEW_FIELDS]
    done = run(argv)
    if done.returncode != 0:
        error = f"gh pr view failed: {' '.join(done.stderr.split())}"
        raise SystemExit(error)
    return parse_pull_request(done.stdout, AGENT_EMAIL)


def fetch_reviews(target: str, number: int, head: str) -> list[Review]:
    """Read review authors with their GitHub account type."""
    if not NAME_WITH_OWNER.fullmatch(target):
        error = "not merging: can't tell which repository the PR is in"
        raise SystemExit(error)
    owner, repo = target.split("/", 1)
    done = run([
        find_tool(GH), "api", "graphql", "-f", f"query={REVIEWS_QUERY}",
        "-f", f"owner={owner}", "-f", f"repo={repo}", "-F", f"number={number}",
    ])  # fmt: skip
    if done.returncode != 0:
        error = f"gh review lookup failed: {' '.join(done.stderr.split())}"
        raise SystemExit(error)
    return review_snapshot(done.stdout, head)


def verify_head(
    number: int, expected: str | None
) -> tuple[PullRequest, str, list[object]]:
    request = fetch(number)
    prefix = f"not merging PR #{number}: "
    if reasons := refusals(request) or approval_reasons(request, expected):
        error = prefix + "; ".join(reasons)
        raise SystemExit(error)
    target = target_of_pr(request.url)
    try:
        checks = [
            check_entry(entry)
            for entry in view_entries(request.rollup, "statusCheckRollup")
        ]
        messages = commit_messages(
            view_entries(request.commits, "commits"), request.head
        )
        reviews = fetch_reviews(target, number, request.head)
        pull = Pull(request.head, reviews, checks, messages)
        if reasons := evaluate(REVIEWS_APP, pull, ATTRIBUTION):
            error = prefix + "; ".join(reasons)
            raise SystemExit(error)
        rules = branch_rules(target, request.base)
        if reasons := required_checks(rules, pull):
            error = prefix + "; ".join(reasons)
            raise SystemExit(error)
    except EvidenceError as exc:
        error = prefix + str(exc)
        raise SystemExit(error) from exc
    return request, target, rules


def check_repository(target: str) -> None:
    """Read the repository's merge setting and require it to be enabled."""
    done = run([find_tool(GH), "repo", "view", target, "--json", MERGE_FIELD])
    if done.returncode != 0:
        error = f"gh repo view failed: {' '.join(done.stderr.split())}"
        raise SystemExit(error)
    require_merge_commits(done.stdout, target)


def branch_rules(target: str, base: str) -> list[object]:
    """Read every page of active rules for the base branch."""
    if not base:
        error = "not merging: gh pr view returned no base branch"
        raise SystemExit(error)
    endpoint = f"repos/{target}/rules/branches/{quote(base, safe='')}"
    done = run([find_tool(GH), "api", "--paginate", "--slurp", endpoint])
    if done.returncode != 0:
        error = f"gh branch rules lookup failed: {' '.join(done.stderr.split())}"
        raise SystemExit(error)
    return parse_branch_rules(done.stdout)


def queue_state(target: str, number: int) -> dict[str, object]:
    """Read the PR's queue status."""
    owner, repo = target.split("/", 1)
    done = run([
        find_tool(GH), "api", "graphql", "-f", f"query={QUEUE_QUERY}",
        "-f", f"owner={owner}", "-f", f"repo={repo}", "-F", f"number={number}",
    ])  # fmt: skip
    if done.returncode != 0:
        error = f"gh merge queue lookup failed: {' '.join(done.stderr.split())}"
        raise SystemExit(error)
    return parse_queue_state(done.stdout)
