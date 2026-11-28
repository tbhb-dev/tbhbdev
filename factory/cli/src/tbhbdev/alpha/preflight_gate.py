# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read the preflight result for a commit and log the push decision."""

import os
import subprocess  # nosec B404
import sys
from contextlib import suppress
from pathlib import Path

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.repo import git

REFUSE = False
RUN_LOG_ENV = "TBHBDEV_RUN_LOG"
PREFLIGHT = "lib/rules/lint/preflight/preflight.py"
RERUN = "; run: bazelisk run //:preflight"


def _record_problem(sha: str, tree: str, path: Path, missing: str) -> str:
    stored_tree, base, result = path.read_text(encoding="utf-8").splitlines()
    if stored_tree != f"tree {tree}":
        return f"record for {sha} names a different tree{RERUN}"
    if not base.startswith("base "):
        return missing
    if result == "result pass":
        return ""
    if result.startswith("result fail "):
        names = result.removeprefix("result fail ")
        return f"preflight failed at {sha}: {names}{RERUN}"
    return missing


def _problem(sha: str) -> str | None:
    """Return None when the commit is exempt from preflight."""
    missing = f"no preflight result for {sha}{RERUN}"
    try:
        code, location = git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", f"{sha}^{{tree}}"
        )
        if code:
            return missing
        common, tree = location.rsplit("\n", 1)
        probe = subprocess.run(  # nosec B603 B607
            ["git", "cat-file", "-e", f"{sha}:{PREFLIGHT}"],
            capture_output=True,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
        if probe.returncode:
            # Git also diagnoses when the missing path exists in the worktree.
            absent = f"fatal: path '{PREFLIGHT}' ".encode()
            return None if probe.stderr.startswith(absent) else missing
        record = Path(common) / "tbhbdev-preflight" / sha
        return _record_problem(sha, tree, record, missing)
    except OSError, ValueError:
        return missing


def problem(sha: str) -> str:
    """Explain why this commit lacks a passing preflight result, if required."""
    return _problem(sha) or ""


def report(sha: str, reason: str) -> None:
    verdict = "refused" if REFUSE else "would refuse"
    detail = f"{verdict} {sha}: {reason}" if reason else f"pass {sha}"
    line = f"push-gate: {detail}\n"
    with suppress(OSError, ValueError):
        sys.stderr.write(line)
    if path := os.environ.get(RUN_LOG_ENV):
        with suppress(OSError, ValueError):
            runfiles.append_text(Path(path), line)


def check(sha: str) -> str:
    """Report the preflight result and return any reason to refuse the operation."""
    reason = _problem(sha)
    if reason is None:
        return ""
    report(sha, reason)
    return reason if REFUSE else ""
