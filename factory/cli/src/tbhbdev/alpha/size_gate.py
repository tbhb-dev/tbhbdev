"""PR size counts and sizing verdicts."""

import json
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast

DATA = Path(__file__).with_name("size_gate_data")
COUNT_KEYS = ("code_lines", "code_limit", "raw_lines", "raw_limit")
SIZE_REVIEW = "size-review"


@dataclass(frozen=True)
class SizeCounts:
    code_lines: int
    code_limit: int
    raw_lines: int
    raw_limit: int
    failed: str | None
    imported_lines: int | None = None
    branch_exempt: bool = False


class SizeCountError(Exception):
    """The counter could not run or returned an invalid result."""


def print_counts(command: str, counts: SizeCounts, branch: str) -> None:
    """Print size totals for check, imports and exempt branches."""
    output = sys.stdout if command == "check" else sys.stderr
    if counts.imported_lines is not None:
        print(
            f"{counts.imported_lines} added lines verified by import maps", file=output
        )
    if command == "check" or counts.imported_lines is not None or counts.branch_exempt:
        print(f"{counts.code_lines} code lines, limit {counts.code_limit}", file=output)
        print(f"{counts.raw_lines} raw lines, limit {counts.raw_limit}", file=output)
    if counts.branch_exempt:
        print(
            f"Branch {branch} is exempt from the size limit by "
            ".github/pr-size-exempt-branches.txt",
            file=output,
        )


def size_counts(  # noqa: PLR0913 -- refs, branch name and counter tools are independent inputs.
    base: str,
    head: str,
    *,
    default_ref: str | None = None,
    head_branch: str = "",
    scc: str = "scc",
    git: str = "git",
    env: dict[str, str] | None = None,
) -> SizeCounts:
    """Count added lines, excluding shared default-branch changes if given.

    Raises:
        SizeCountError: The command is unavailable, fails or returns bad JSON.
    """
    invoke = (
        subprocess.run if env is None else partial(subprocess.run, env=env, timeout=10)
    )
    try:
        done = invoke(  # nosec B603 B607
            [
                "bash",
                str(DATA / "pr-size.sh"),
                base,
                head,
                scc,
                git,
                str(DATA / "pr-size-exempt.txt"),
                default_ref or "",
                sys.executable,
                head_branch,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise SizeCountError(str(exc)) from exc
    if done.returncode:
        message = done.stderr.strip() or f"counter exited {done.returncode}"
        raise SizeCountError(message)
    try:
        data: object = json.loads(done.stdout)
    except ValueError as exc:
        message = "counter returned invalid JSON"
        raise SizeCountError(message) from exc
    if (
        not isinstance(data, dict)
        or any(type(data.get(key)) is not int for key in COUNT_KEYS)
        or type(data.get("branch_exempt")) is not bool
        or "imported_lines" not in data
        or (
            data["imported_lines"] is not None
            and type(data["imported_lines"]) is not int
        )
        or "failed" not in data
        or cast(
            "object",  # pragma: no mutate -- Type-only argument.
            data["failed"],
        )
        not in ("code", "raw", None)
    ):
        message = "counter returned invalid size counts"
        raise SizeCountError(message)
    return SizeCounts(
        *(
            cast(
                "int",  # pragma: no mutate -- Type-only argument.
                data[key],
            )
            for key in COUNT_KEYS
        ),
        failed=cast(
            "str | None",  # pragma: no mutate -- Type-only argument.
            data["failed"],
        ),
        imported_lines=data["imported_lines"],
        branch_exempt=data["branch_exempt"],
    )


def size_verdict(line: str, head: str) -> str | None:
    """Return the sizing verdict from a final line that names the pinned head."""
    if line == f"VERDICT: SPLIT {head}":
        return "SPLIT"
    if line == f"VERDICT: OVERRIDE {head}":
        return "OVERRIDE"
    return None
