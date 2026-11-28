"""Interpret branch and comparison values for build publication."""

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
