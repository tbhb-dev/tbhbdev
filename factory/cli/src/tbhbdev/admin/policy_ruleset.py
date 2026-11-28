"""Pure rules for preserving repository and branch protections."""

import re
from typing import cast

from tbhbdev.admin import policy_paths

RULESET_KEYS = {"name", "target", "enforcement", "conditions", "bypass_actors", "rules"}
RULESET_PLAIN_RULES = {
    "deletion",
    "non_fast_forward",
    "required_linear_history",
    "required_signatures",
}
# From .github/rulesets/main.json: one approval, merge or squash only (squash
# stays allowed during the move to merge commits), the Actions app.
RULESET_APPROVALS = 1
RULESET_MERGE_METHODS = frozenset({"merge", "squash"})
RULESET_DEFAULT_METHODS = ["squash"]
RULESET_CHECKS_APP = 15368
OFF_DISMISSAL: dict[str, object] = {"enabled": False, "allowed_actors": []}
RULESET_REVIEW_KEYS = {
    "required_approving_review_count", "dismiss_stale_reviews_on_push",
    "required_reviewers", "require_code_owner_review", "dismissal_restriction",
    "require_last_push_approval", "required_review_thread_resolution",
    "require_extra_approval_for_unattributed_changes", "allowed_merge_methods",
}  # fmt: skip
RULESET_CONDITIONS: dict[str, dict[str, list[str]]] = {
    "ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}
}


def safe_dismissal(value: object) -> bool:
    """A dismissal restriction that is off with no actors, or absent."""
    return (
        isinstance(value, dict)
        and value == OFF_DISMISSAL
        and value["enabled"] is False  # 0 equals False, so check the type
    )


def safe_methods(methods: object) -> bool:
    """A non-empty list of distinct merge methods from merge and squash."""
    return (
        isinstance(methods, list)
        and bool(methods)
        and len(set(methods)) == len(methods)
        and set(methods) <= RULESET_MERGE_METHODS
    )


def safe_pull_request_rule(rule: dict[str, object]) -> bool:
    """A pull request rule with one approval, no code owner or team requirement."""
    parameters = rule.get("parameters")
    if rule.keys() != {"type", "parameters"} or not isinstance(parameters, dict):
        return False
    options: dict[str, object] = parameters
    count = options.get("required_approving_review_count")
    methods = options.get("allowed_merge_methods", RULESET_DEFAULT_METHODS)
    return (
        isinstance(count, int)
        and not isinstance(count, bool)
        and 1 <= count <= RULESET_APPROVALS
        and safe_methods(methods)
        and set(options) <= RULESET_REVIEW_KEYS
        and options.get("require_code_owner_review", False) is False
        and options.get("required_reviewers", []) == []
        and safe_dismissal(options.get("dismissal_restriction", OFF_DISMISSAL))
    )


def safe_status_check(entry: object) -> bool:
    """A `conclusion` check, bound to no app or to the Actions app."""
    if not isinstance(entry, dict):
        return False
    item: dict[str, object] = entry
    return (
        item.get("context") == "conclusion"
        and set(item) <= {"context", "integration_id"}
        and type(item.get("integration_id", RULESET_CHECKS_APP)) is int
        and item.get("integration_id", RULESET_CHECKS_APP) == RULESET_CHECKS_APP
    )


def safe_status_checks_rule(rule: dict[str, object]) -> bool:
    """A status check rule that names only `conclusion`."""
    parameters = rule.get("parameters")
    if rule.keys() != {"type", "parameters"} or not isinstance(parameters, dict):
        return False
    options: dict[str, object] = parameters
    checks = options.get("required_status_checks")
    if not isinstance(checks, list) or not checks:
        return False
    entries: list[object] = checks
    return all(safe_status_check(entry) for entry in entries)


def safe_ruleset_rule(rule: object) -> bool:
    """One rule type from the live `main` ruleset, in its restrictive form."""
    if not isinstance(rule, dict):
        return False
    item: dict[str, object] = rule
    kind = item.get("type")
    if not isinstance(kind, str):
        return False
    if kind in RULESET_PLAIN_RULES:
        return item.keys() == {"type"}
    if kind == "pull_request":
        return safe_pull_request_rule(item)
    return kind == "required_status_checks" and safe_status_checks_rule(item)


def safe_ruleset_create(body: dict[str, object]) -> bool:
    """A new active branch ruleset with no bypass actor and only the live rules.

    A ruleset can only add restrictions, so creating one never weakens a repository.
    """
    rules = body.get("rules")
    return (
        set(body) <= RULESET_KEYS
        and body.get("target") == "branch"
        and body.get("enforcement") == "active"
        and body.get("bypass_actors") == []
        and body.get("conditions", RULESET_CONDITIONS) == RULESET_CONDITIONS
        and isinstance(rules, list)
        and bool(rules)
        and all(
            safe_ruleset_rule(rule)
            for rule in cast("list[object]", rules)  # pragma: no mutate
        )
    )


def protection(method: str, path: str, body: dict[str, object]) -> bool:
    """Only operations that can be classified without previous state pass."""
    if method == "POST" and re.fullmatch(rf"{policy_paths.REPO}/rulesets", path):
        return safe_ruleset_create(body)
    if "/rulesets" in path:
        return method in {"PUT", "PATCH"} and body == {"enforcement": "active"}
    return (
        method == "POST"
        and path.endswith(("/required_signatures", "/enforce_admins"))
        and not body
    )
