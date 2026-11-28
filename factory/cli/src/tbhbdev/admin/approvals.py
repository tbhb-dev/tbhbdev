"""Compare a repository ruleset update against the operator's approvals."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.admin.policy_ruleset import RULESET_KEYS

if TYPE_CHECKING:
    from collections.abc import Sequence

RESPONSE_METADATA = {
    "id",
    "source",
    "source_type",
    "node_id",
    "_links",
    "created_at",
    "updated_at",
    "current_user_can_bypass",
}


@dataclass(frozen=True)
class Approval:
    repository: str
    ruleset_id: int
    rule: str
    parameter: str
    value: object
    decision: str


def for_path(path: str, entries: Sequence[Approval]) -> tuple[Approval, ...]:
    return tuple(
        entry
        for entry in entries
        if path == f"/repos/{entry.repository}/rulesets/{entry.ruleset_id}"
    )


def same_value(left: object, right: object) -> bool:
    """Compare parsed values without treating booleans as numbers."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        left_fields: dict[str, object] = left
        right_fields: dict[str, object] = right
        return left_fields.keys() == right_fields.keys() and all(
            same_value(value, right_fields[key]) for key, value in left_fields.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        left_items: list[object] = left
        right_items: list[object] = right
        return len(left_items) == len(right_items) and all(
            same_value(a, b) for a, b in zip(left_items, right_items, strict=True)
        )
    return left == right


def rule_decisions(
    before: object, after: object, entries: Sequence[Approval]
) -> set[str] | None:
    if same_value(before, after):
        return set()
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or before.keys() != {"type", "parameters"}
        or after.keys() != before.keys()
        or before["type"] != after["type"]
    ):
        return None
    old: object = before["parameters"]
    new: object = after["parameters"]
    if (
        not isinstance(old, dict)
        or not isinstance(new, dict)
        or not old.keys() <= new.keys()
    ):
        return None
    old_options: dict[str, object] = old
    new_options: dict[str, object] = new
    decisions: set[str] = set()
    for parameter, value in new_options.items():
        if parameter in old_options and same_value(old_options[parameter], value):
            continue
        decision = next(
            (
                entry.decision
                for entry in entries
                if entry.rule == before["type"]
                and entry.parameter == parameter
                and same_value(entry.value, value)
                and entry.decision
            ),
            None,
        )
        if decision is None:
            return None
        decisions.add(decision)
    return decisions


def approved_decisions(
    path: str, current: object, body: object, entries: Sequence[Approval]
) -> tuple[str, ...]:
    """Return the decisions covering a complete update, or an empty refusal."""
    candidates = for_path(path, entries)
    if not candidates or not isinstance(current, dict) or not isinstance(body, dict):
        return ()
    current_fields: dict[str, object] = current
    wanted_fields: dict[str, object] = body
    target = candidates[0]
    source = current_fields.get("source")
    if (
        current_fields.get("source_type") != "Repository"
        or not isinstance(source, str)
        or source.lower() != target.repository
        or not same_value(current_fields.get("id"), target.ruleset_id)
        or current_fields.keys() - RESPONSE_METADATA != RULESET_KEYS
        or wanted_fields.keys() != RULESET_KEYS
        or not isinstance(current_fields["bypass_actors"], list)
    ):
        return ()
    if not all(
        same_value(current_fields[key], wanted_fields[key])
        for key in RULESET_KEYS - {"rules"}
    ):
        return ()
    before, after = current_fields["rules"], wanted_fields["rules"]
    if (
        not isinstance(before, list)
        or not isinstance(after, list)
        or len(before) != len(after)
    ):
        return ()
    current_rules: list[object] = before
    wanted_rules: list[object] = after
    decisions: set[str] = set()
    for old, new in zip(current_rules, wanted_rules, strict=True):
        matched = rule_decisions(old, new, candidates)
        if matched is None:
            return ()
        decisions.update(matched)
    return tuple(sorted(decisions))
