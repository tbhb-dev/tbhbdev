# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse lint output and prose configuration."""

import json
import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from tbhbdev.alpha.pull_request.body import Problem

# Vale's built-in style, which no package provides.
BUILTIN_STYLE = "Vale"


# The .vale.ini key that names styles, compared without regard to case.
BASED_ON = "BasedOnStyles"
RUMDL_LINE = re.compile(r"\S+?:(\d+):\d+: (.+)")
CSPELL_LINE = re.compile(r"\S+?:(\d+):\d+ - (.+)")


def parse_vale(output: str) -> list[Problem]:
    """Vale's JSON alerts."""
    data = cast(
        "dict[str, list[dict[str, object]]]", json.loads(output)
    )  # pragma: no mutate
    invalid = "expected Vale alerts grouped by file"
    if not isinstance(data, dict) or any(
        not isinstance(alerts, list) for alerts in data.values()
    ):
        raise ValueError(invalid)
    try:
        return [
            (int(str(alert["Line"])), f"vale {alert['Check']}: {alert['Message']}")
            for alerts in data.values()
            for alert in alerts
        ]
    except (KeyError, TypeError) as exc:
        raise ValueError(invalid) from exc


def parse_lines(pattern: re.Pattern[str], tool: str) -> Callable[[str], list[Problem]]:
    """A parser for a tool that prints one `path:line:col...` finding per line."""

    def parse(output: str) -> list[Problem]:
        found = (pattern.fullmatch(line) for line in output.splitlines())
        return [(int(match[1]), f"{tool}: {match[2]}") for match in found if match]

    return parse


def parse_styles(text: str) -> list[str]:
    """The styles .vale.ini's ``BasedOnStyles`` lines name, in any section."""
    named: list[str] = []
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == BASED_ON.lower():
            named += (name.strip() for name in value.split(","))
    return sorted({name for name in named if name and name != BUILTIN_STYLE})


def title_findings(title: str, limit: int, status: int, stderr: str) -> list[str]:
    """Report title length and Conventional Commit verification findings."""
    problems = []
    if len(title) > limit:
        problems.append(f"title is {len(title)} characters; the limit is {limit}")
    if status != 0:
        why = " ".join(stderr.split())
        problems.append(
            f"title is not a Conventional Commit header (cog verify): {why}"
        )
    return problems


def lint_findings(
    name: str,
    parse: Callable[[str], list[Problem]],
    status: int,
    stdout: str,
    stderr: str,
) -> list[Problem]:
    """Report parsed linter findings or the command's failure."""
    if status in {0, 1}:
        try:
            found = parse(stdout)
        except ValueError:
            return [(1, f"{name} failed (exit {status}): invalid JSON")]
        # Exit 1 requires findings. Exit 0 can report a clean result.
        if status == 0 or found:
            return found
    why = " ".join((stderr or stdout).split())
    return [(1, f"{name} failed (exit {status}): {why}")]
