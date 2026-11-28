# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Validate repository settings and active branch rules."""

import json
from typing import TYPE_CHECKING, cast

from tbhbdev.merge.evidence import EvidenceError, object_fields

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tbhbdev.merge.review_gate import Pull

MERGE_FIELD = "mergeCommitAllowed"


def require_merge_commits(output: str, target: str) -> None:
    """Require the repository to allow merge commits."""
    try:
        raw: object = json.loads(output)
    except ValueError:
        error = "gh repo view returned no usable merge settings"
        raise SystemExit(error) from None
    data = object_fields(raw) or {}
    allowed = data.get(MERGE_FIELD)
    if not isinstance(allowed, bool):
        error = "gh repo view returned no usable merge settings"
        raise SystemExit(error)
    if not allowed:
        error = f"not merging: {target} does not allow merge commits"
        raise SystemExit(error)


def parse_branch_rules(output: str) -> list[object]:
    """Flatten the pages of active rules for the base branch."""
    try:
        pages: object = json.loads(output)
    except ValueError:
        error = "gh returned no usable branch rules"
        raise SystemExit(error) from None
    if not isinstance(pages, list) or not pages:
        error = "gh returned no usable branch rules"
        raise SystemExit(error)
    rules: list[object] = []
    for page in cast("list[object]", pages):
        if not isinstance(page, list):
            error = "gh returned no usable branch rules"
            raise SystemExit(error)
        rules.extend(cast("list[object]", page))
    return rules


def required_checks(rules: Sequence[object], pull: Pull) -> list[str]:
    """Find required ruleset contexts that lack a successful result at the head."""
    green = {
        check["name"]
        for check in pull.checks
        if check["status"] == "completed" and check["conclusion"] == "success"
    }
    return [
        f"required check {name} has no success at the head"
        for row in rules
        if (rule := object_fields(row)) and rule.get("type") == "required_status_checks"
        for name in required_contexts(rule.get("parameters"))
        if name not in green
    ]


def required_contexts(parameters: object) -> list[str]:
    fields = object_fields(parameters) or {}
    checks = fields.get("required_status_checks")
    if not isinstance(checks, list) or not checks:
        message = "gh returned no usable required status checks"
        raise EvidenceError(message)
    names = []
    for row in cast("list[object]", checks):
        context = object_fields(row) or {}
        name = context.get("context")
        if not isinstance(name, str) or not name:
            message = "gh returned an invalid required status check"
            raise EvidenceError(message)
        names.append(name)
    return names


def requires_queue(rules: Sequence[object]) -> bool:
    """Whether the base branch has a merge queue using merge commits."""
    queued = False
    for entry in rules:
        rule = object_fields(entry)
        if rule is None or not isinstance(rule.get("type"), str):
            error = "gh returned no usable branch rules"
            raise SystemExit(error)
        if rule["type"] == "merge_queue":
            parameters = object_fields(rule.get("parameters")) or {}
            method = str(parameters.get("merge_method", "(missing)")).upper()
            if method != "MERGE":
                error = f"not merging: unsupported branch merge queue method {method}"
                raise SystemExit(error)
            queued = True
    return queued
