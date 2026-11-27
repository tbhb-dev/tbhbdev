# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Scan pull request text, create the pull request, and record its opener."""

import sys

from tbhbdev.alpha import opened, secret_scan
from tbhbdev.alpha.pull_request import github as pr_github
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.publication import (
    created_ref,
    creation_errors,
    existing_pr_problem,
)
from tbhbdev.alpha.pull_request.tools import GH, GITLEAKS


def scan_secrets(text: str, what: str, visibility: str) -> None:
    """Refuse text that gitleaks flags; see ``tbhbdev.alpha.secret_scan``.

    ``visibility`` is that of the repository the text is posted to, which the
    caller reads once per command, with ``repo_visibility`` or
    ``base_visibility``.
    """
    secret_scan.scan(
        text,
        what,
        root=pr_tools.root(),
        gitleaks=pr_tools.find_tool(GITLEAKS, unwrapped=True),
        visibility=visibility,
    )


def post_text(title: str, body: str, visibility: str) -> None:
    """Scan the PR title and body as gh would post them."""
    scan_secrets(f"{title}\n\n{body}", "the PR title and body", visibility)


def create_pr(argv: list[str], body: str, root: str | None, *, dry_run: bool) -> int:
    """Refuse an existing open PR, including a race reported by gh create."""
    done = pr_tools.run(
        [pr_tools.find_tool(GH), "pr", "view", "--json", "number,state,url"]
    )
    data = pr_github.gh_data(done)
    problem = existing_pr_problem(data, done.returncode, done.stdout, done.stderr)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    if dry_run:
        return pr_github.invoke_gh(argv, body, dry_run=True)[0]
    argv[0] = pr_tools.find_tool(GH)
    done = pr_tools.run(argv, stdin=body)
    errors = creation_errors(done.returncode, done.stdout, done.stderr)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    if root is None:
        return 0
    return record_created(root, done.stdout)


def record_created(root: str, stdout: str) -> int:
    """Add the PR that gh printed to the opener's ledger; the exit code.

    Once the PR exists, a ledger that can't be written only warns.
    """
    ref = created_ref(stdout)
    if ref is None:
        print("gh printed no pull request URL; nothing was recorded", file=sys.stderr)
        return 1
    try:
        opened.record_opened(root, [ref])
    except (opened.LedgerError, OSError) as exc:
        message = f"warning: the PR is open, but the ledger was not updated: {exc}"
        print(message, file=sys.stderr)
    return 0
