# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read and write GitHub pull request data through gh."""

import json
import shlex
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import subprocess  # nosec B404
    from collections.abc import Sequence
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.tools import GH


def gh_data(
    done: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
) -> object:
    """The JSON that gh printed, or None when gh failed or printed none."""
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def invoke_gh(
    argv: list[str], body: str, *, dry_run: bool = False, quiet: bool = False
) -> tuple[int, str]:
    """Run gh, or show what would run. Pass output and errors through unless quiet.

    Returns gh's exit code and its standard output.
    """
    if dry_run:
        print(shlex.join(argv))
        print()
        print(body, end="")
        return 0, ""
    argv[0] = pr_tools.find_tool(GH)
    done = pr_tools.run(argv, stdin=body)
    if not quiet:
        print(done.stdout, end="")
        print(done.stderr, end="", file=sys.stderr)
    return done.returncode, done.stdout


def gh_json(argv: Sequence[str]) -> list[dict[str, object]]:
    """A gh command's JSON list output.

    Raises:
        SystemExit: gh fails or prints something other than a JSON list.
    """
    done = pr_tools.run([pr_tools.find_tool(GH), *argv])
    data = gh_data(done)
    if not isinstance(data, list):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"gh {' '.join(argv[:2])} failed: {why}"
        raise SystemExit(message)
    return cast("list[dict[str, object]]", data)  # pragma: no mutate


def head_of(number: int) -> str:
    """PR N's head commit.

    Raises:
        SystemExit: gh can't read the pull request.
    """
    done = pr_tools.run(
        [pr_tools.find_tool(GH), "pr", "view", str(number), "--json", "headRefOid"]
    )
    data = gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read the head of PR {number}: {why}"
        raise SystemExit(message)
    return str(cast("dict[str, object]", data)["headRefOid"])  # pragma: no mutate
