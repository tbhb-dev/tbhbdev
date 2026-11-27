# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Format identity hook failure reports and exit statuses."""

from __future__ import annotations

PROG = "tbhbdev alpha identity"


def hook_failure(
    command: str, error_type: str, function: str, line: int | None
) -> tuple[str, int]:
    """Describe where a hook failed using fields safe to include in stderr."""
    message = f"{PROG} {command}: failed: {error_type} in {function} (line {line})"
    return message, 0 if command == "record" else 1
