# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Interpret the GitHub responses to a pull request creation."""

from typing import cast

from tbhbdev.alpha.pull_request.metadata import PR_URL

EDIT_DRAFT = "run `tbhbdev alpha pr edit DRAFT --pr {number}` to apply the draft"


def existing_pr_problem(
    data: object, status: int, stdout: str, stderr: str
) -> str | None:
    """Why the existing pull request lookup prevents creating a new one."""
    if isinstance(data, dict) and all(
        type(data.get(key)) is kind
        for key, kind in (("state", str), ("number", int), ("url", str))
    ):
        data = cast("dict[str, object]", data)  # pragma: no mutate
        if data["state"] == "OPEN":
            return (
                f"error: PR {data['number']} ({data['url']}) is already open; "
                + EDIT_DRAFT.format(number=data["number"])
            )
    elif not (status and "no pull requests found" in stderr.lower()):
        output = (stderr or stdout).rstrip("\n")
        return f"cannot check for an open PR: {output}"
    return None


def creation_errors(status: int, stdout: str, stderr: str) -> list[str]:
    """Diagnostics for a failed creation, including a duplicate reported by gh."""
    output = "\n".join(filter(None, (stdout.rstrip("\n"), stderr.rstrip("\n"))))
    duplicate = "already exists" in output.lower()
    if not (status or duplicate):
        return []
    if not duplicate:
        return [output]
    found = PR_URL.search(output)
    number = found[2] if found else "<n>"
    return [output, "gh pr create failed; " + EDIT_DRAFT.format(number=number)]


def created_ref(stdout: str) -> str | None:
    """Return the ledger reference for the pull request gh opened."""
    found = PR_URL.search(stdout)
    return f"{found[1]}#{found[2]}" if found else None
