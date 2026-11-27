# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read and report the checks on a pull request head."""

import sys

from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request.check_results import check_nodes, check_outcome

PENDING_CODE = 3


# Query the immutable commit, rather than the PR's potentially stale rollup.
# gh follows pageInfo with --paginate; --slurp combines all context pages.
HEAD_CHECKS_CALL = (
    "api",
    "graphql",
    "--paginate",
    "--slurp",
    "--field",
    "owner={owner}",
    "--field",
    "name={repo}",
)


HEAD_CHECKS_QUERY = """
query($owner: String!, $name: String!, $head: GitObjectID!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    object(oid: $head) {
      ... on Commit {
        statusCheckRollup {
          contexts(first: 100, after: $endCursor) {
            nodes {
              __typename
              ... on CheckRun { name status conclusion }
              ... on StatusContext { context state }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
  }
}
"""


def checks(number: int) -> int:
    """Print a PR's head SHA and checks; exit 0 only when all are green.

    Exit 1 for a failed or unknown check, and 3 when checks are absent or
    still unfinished.

    Raises:
        SystemExit: gh can't read the pull request or checks, or a check has no name.
    """
    head = pr_github.head_of(number)
    print(head)
    pages = pr_github.gh_json(
        [
            *HEAD_CHECKS_CALL,
            "--raw-field",
            f"head={head}",
            "--raw-field",
            f"query={HEAD_CHECKS_QUERY}",
        ]
    )
    jobs = [job for page in pages for job in check_nodes(page)]
    if not jobs:
        print(f"no checks on PR {number} at {head}; pending", file=sys.stderr)
        return PENDING_CODE
    # None has the same truth value here; its initialization mutant is equivalent.
    failed = pending = False  # pragma: no mutate
    for job in jobs:
        name, label, kind = check_outcome(job)
        print(f"{head} {name}: {label}")
        pending = pending or kind == "pending"
        failed = failed or kind == "failed"
    if failed:
        return 1
    return PENDING_CODE if pending else 0
