# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Take a pull request out of its base branch's merge queue, and nothing else."""

import argparse
import json
import shlex
from typing import TYPE_CHECKING

from tbhbdev.alpha.pull_request.metadata import target_of_pr
from tbhbdev.alpha.pull_request.tools import GH, find_tool, run
from tbhbdev.merge import write
from tbhbdev.merge.evidence import object_fields
from tbhbdev.merge.github import branch_rules, queue_state
from tbhbdev.merge.rules import has_merge_queue

if TYPE_CHECKING:
    from collections.abc import Sequence

DEQUEUE = """\
mutation($id: ID!) {
  dequeuePullRequest(input: {id: $id}) { clientMutationId }
}
"""
VERB = "merge-queue-remove"


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="Remove a PR from the merge queue of its base branch."
    )
    parser.add_argument("number", type=int, metavar="PR")
    parser.add_argument("--reason", required=True, help="why it leaves the queue")
    parser.add_argument(
        "--dry-run", action="store_true", help="show it, remove nothing"
    )
    return parser


def read_pull_request(number: int) -> dict[str, object]:
    """The PR's node ID, state, base branch and URL."""
    done = run([
        find_tool(GH), "pr", "view", str(number), "--json", "id,state,baseRefName,url",
    ])  # fmt: skip
    if done.returncode != 0:
        error = f"gh pr view failed: {' '.join(done.stderr.split())}"
        raise SystemExit(error)
    try:
        data = object_fields(json.loads(done.stdout))
    except ValueError:
        data = None
    if data is None or not all(
        isinstance(data.get(k), str) and data[k]
        for k in ("id", "state", "baseRefName", "url")
    ):
        error = "gh pr view returned no usable pull request"
        raise SystemExit(error)
    return data


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    """Refuse unless PR N is open and queued, then dequeue it."""
    args = build_parser(prog).parse_args(argv)
    number, reason = args.number, args.reason.strip()
    prefix = f"not removing PR #{number}: "
    if not reason:
        raise SystemExit(prefix + "--reason must not be empty")
    pull = read_pull_request(number)
    if pull["state"] != "OPEN":
        raise SystemExit(prefix + f"it is {pull['state']}, not open")
    target = target_of_pr(pull["url"])
    if not target:
        raise SystemExit(prefix + "can't tell which repository it is in")
    base = str(pull["baseRefName"])
    if not has_merge_queue(branch_rules(target, base)):
        raise SystemExit(prefix + f"{target} has no merge queue for {base}")
    if queue_state(target, number).get("isInMergeQueue") is not True:
        raise SystemExit(prefix + "it is not in the merge queue")
    gh_args = [
        "api", "graphql", "-f", f"query={DEQUEUE}", "-f", f"id={pull['id']}",
    ]  # fmt: skip
    if args.dry_run:
        print(shlex.join(["gh", *gh_args]))
        return 0
    done = write.gh_write(VERB, (target, number), gh_args, reason=reason)
    if done.returncode != 0:
        error = f"gh dequeue failed: {' '.join(done.stderr.split())}"
        raise SystemExit(error)
    print(f"PR #{number} removed from the merge queue of {base}: {reason}")
    return 0
