# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Validate pull request branch actions and their text."""

import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

DRIVER_APP_AUTHOR = "app/tbhb-dev-agent-pull-requests"

REBASE_MARKER = "<!-- rebase-check -->"
REBASE_UNCHECKED = "- [ ]"
REBASE_CHECKED = "- [x]"
SECRET_SHAPED = re.compile(r"\S+://\S+|\b[A-Za-z0-9_-]{20,}\b")
WHY_LIMIT = 200


def refusal_reason(*raw: bytes) -> str:
    """The captured stderr, with URLs and long token-like words redacted and cut."""
    # Python spells the codec error handler case-blind, so no test can tell "REPLACE".
    text = " ".join(
        b" ".join(raw).decode(errors="replace").split()  # pragma: no mutate
    )
    return SECRET_SHAPED.sub("<redacted>", text)[:WHY_LIMIT]


def tick_rebase_checkbox(body: str, line: str) -> str:
    """Tick the first checkbox on the unique line containing the marker."""
    return body.replace(line, line.replace(REBASE_UNCHECKED, REBASE_CHECKED, 1))


def rebase_body(
    number: int,
    body: str,
    *,
    replace_checkbox: Callable[[str, str], str] = tick_rebase_checkbox,
) -> str | None:
    """Check the marker and verify the rewritten body, including line endings.

    Raises:
        SystemExit: The checkbox is ambiguous, missing, or unsafe to change.
    """
    lines = body.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if REBASE_MARKER in line]
    if not matches:
        msg = f"PR {number}: no Renovate rebase checkbox line"
        raise SystemExit(msg)
    if len(matches) > 1:
        msg = f"PR {number}: multiple Renovate rebase checkbox lines"
        raise SystemExit(msg)
    (index,) = matches
    line = lines[index]
    if line.startswith(REBASE_CHECKED):
        return None
    if not line.startswith(REBASE_UNCHECKED):
        msg = f"PR {number}: malformed Renovate rebase checkbox line"
        raise SystemExit(msg)
    updated = replace_checkbox(body, line)
    # The prefix through the changed line and the suffix cover the entire body.
    prefix = "".join(lines[:index]) + REBASE_CHECKED + line[len(REBASE_UNCHECKED) :]
    if (
        len(updated) != len(body)
        or not updated.startswith(prefix)
        or not updated.endswith("".join(lines[index + 1 :]))
    ):
        msg = f"PR {number}: cannot verify a checkbox-only change"
        raise SystemExit(msg)
    return updated


def own_author_problem(
    number: int, author: bytes, current: bytes | None, *errors: bytes
) -> str | None:
    """Why a pull request cannot be marked ready by this account."""
    if (
        current is not None
        and author.strip()
        and (current == author or author.strip() == DRIVER_APP_AUTHOR.encode())
    ):
        return None
    why = refusal_reason(*errors)
    message = f"not marking PR {number} ready: its author isn't this agent's account"
    return f"{message}: {why}" if why else message


def branch_to_delete(number: int, data: object) -> str:
    """The branch of a finished pull request that may be checked for deletion."""
    if not isinstance(data, dict):
        message = f"cannot read PR {number} on origin"
        raise SystemExit(message)
    data = cast("dict[str, object]", data)  # pragma: no mutate
    if data.get("state") not in ("CLOSED", "MERGED"):
        message = f"PR {number} is not closed or merged"
        raise SystemExit(message)
    if data.get("isCrossRepository") is not False:
        message = f"PR {number} head is not on origin"
        raise SystemExit(message)
    branch = data.get("headRefName")
    if not isinstance(branch, str) or not branch:
        message = f"cannot read the head branch of PR {number}"
        raise SystemExit(message)
    if branch == "main":
        message = "refusing to delete main"
        raise SystemExit(message)
    return branch


def require_unprotected(branch: str, data: object) -> None:
    """Refuse to delete a branch unless its protection state is known."""
    if (
        not isinstance(data, dict)
        or data.get("name") != branch
        or data.get("protected") is not False
    ):
        message = f"branch {branch} is protected or unreadable"
        raise SystemExit(message)
