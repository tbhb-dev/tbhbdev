"""Interpret branch, comparison and pull request values for build publication."""

import re


def branch_name(run_name: str, start_ref: str | None) -> str:
    if start_ref is not None:
        return start_ref.removeprefix("refs/heads/")
    return f"agent/{run_name}"


def ref_tip(record: object, ref: str) -> str | None:
    if not isinstance(record, dict) or record.get("ref") != ref:
        return None
    target = record.get("object")
    tip = target.get("sha") if isinstance(target, dict) else None
    return tip if isinstance(tip, str) and re.fullmatch(r"[0-9a-f]{40}", tip) else None


def origin_ahead(record: object) -> bool | None:
    return record.get("status") == "ahead" if isinstance(record, dict) else None


def pull_number(record: object) -> str | None:
    """Return the first pull request number. Empty means absent, None unreadable."""
    if not isinstance(record, list):
        return None
    if not record:
        return ""
    pull: object = record[0]
    number: object = pull.get("number") if isinstance(pull, dict) else None
    return str(number) if type(number) is int and number > 0 else None
