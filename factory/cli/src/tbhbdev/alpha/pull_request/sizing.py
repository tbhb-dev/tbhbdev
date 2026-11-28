# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Count the draft pull request changes against its base branch."""

import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import size_gate
from tbhbdev.alpha.pull_request import repository as pr_repository
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.tools import SCC

if TYPE_CHECKING:
    import argparse

    from tbhbdev.alpha.pull_request.draft import Checked


def size_unavailable(checked: Checked, reason: str) -> None:
    message = f"size count unavailable: {reason}"
    checked.problems.append((1, message))
    print(message, file=sys.stderr)


def _stacked_base_source(branch: str) -> str:
    """Use the build mirror's fetched ref when it has no ordinary branch ref."""
    source = f"refs/heads/{branch}"
    code, origin = pr_tools.git("remote", "get-url", "origin")
    if code != 0 or not Path(origin).is_absolute():
        return source
    mirror_source = f"refs/heads/fetched/heads/{branch}"
    code, advertised = pr_tools.git("ls-remote", "origin", source, mirror_source)
    refs = {line.partition("\t")[2] for line in advertised.splitlines()}
    if code == 0 and source not in refs and mirror_source in refs:
        return mirror_source
    return source


def _size_base(
    branch: str, *, explicit_base: bool, dry_run: bool
) -> tuple[str, str | None]:
    """Refresh the base and any stacked default ref together, except in a dry run."""
    base = f"refs/remotes/origin/{branch}"
    default = branch
    if explicit_base:
        with contextlib.suppress(SystemExit):
            default = pr_repository.default_base(origin_only=True)
    default_ref = f"refs/remotes/origin/{default}" if default != branch else None
    if not dry_run:
        source = _stacked_base_source(branch) if default_ref else f"refs/heads/{branch}"
        fetch = (
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "--refmap=",
            "origin",
            f"+{source}:{base}",
        )
        if default_ref is not None:
            fetch += (f"+refs/heads/{default}:{default_ref}",)
        if pr_tools.git(*fetch)[0] != 0:
            message = f"cannot fetch {branch}"
            raise size_gate.SizeCountError(message)
    if pr_tools.git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")[0] != 0:
        message = f"base branch {base} not found"
        raise size_gate.SizeCountError(message)
    return base, default_ref


def size_excess(counts: size_gate.SizeCounts) -> str:
    """Give the same refusal for any pull request that exceeds the limit."""
    count, limit = (
        (counts.code_lines, counts.code_limit)
        if counts.failed == "code"
        else (counts.raw_lines, counts.raw_limit)
    )
    return f"{count} lines, limit {limit}: split into stacked PRs"


def _report_size_excess(
    draft: str, checked: Checked, counts: size_gate.SizeCounts, *, dry_run: bool
) -> None:
    """Report a size excess as a draft problem or a dry-run warning."""
    why = size_excess(counts)
    if dry_run:
        print(f"warning: {draft}:1: {why}", file=sys.stderr)
        return
    checked.problems.append((1, why))
    print(f"{draft}:1: {why}")


def check_size(args: argparse.Namespace, checked: Checked) -> None:
    """Count added lines and refuse an unavailable count, including in a dry run."""
    explicit_base = "base" in checked.meta
    branch = pr_repository.base_of(checked.meta)
    checked.meta.setdefault("base", branch)
    dry_run = getattr(
        args,
        "dry_run",
        False,  # pragma: no mutate -- None is also false.
    )
    if args.command == "create" and not pr_repository.target_is_origin():
        # gh may open the PR against a repository whose base origin doesn't track.
        size_unavailable(checked, "gh may target a repository other than origin")
        return
    try:
        base, default_ref = _size_base(
            branch, explicit_base=explicit_base, dry_run=dry_run
        )
        _, head_branch = pr_tools.git("branch", "--show-current")
        counts = size_gate.size_counts(
            base,
            "HEAD",
            default_ref=default_ref,
            head_branch=head_branch,
            scc=pr_tools.find_tool(SCC),
            git=pr_tools.git_executable(),
        )
    except size_gate.SizeCountError as exc:
        size_unavailable(checked, str(exc))
        return
    size_gate.print_counts(args.command, counts, head_branch)
    if counts.failed is not None:
        _report_size_excess(args.draft, checked, counts, dry_run=dry_run)
