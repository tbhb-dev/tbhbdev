# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Validate the syntax and order of commit identity trailers."""

from __future__ import annotations

import re

from tbhbdev.alpha.identity.models import EFFORTS, SLUGS
from tbhbdev.alpha.identity.records import HASH_RE
from tbhbdev.alpha.identity.trailers import ATTR_ORDER, KEY_ASSIST, KEY_GEN, start_ok

REQUIRED = ("harness", "model")


LINE = re.compile(r"^(Generated-by|Assisted-by):\s*(.*)$")


PAIR = re.compile(r"[a-z_]+=[^\s;,]+")


def check_pair(pair: str) -> str | None:
    """Why one ``key=value`` attribute is invalid, or None."""
    if not PAIR.fullmatch(pair):
        return f"bad attribute {pair!r}"
    key, _, value = pair.partition("=")
    problem = None
    if key not in ATTR_ORDER:
        problem = f"unknown attribute {key}"
    elif key == "effort" and value not in EFFORTS:
        problem = f"bad effort {value}"
    elif key in {"launch", "session", "subagent"} and not HASH_RE.fullmatch(value):
        problem = f"bad {key} hash"
    elif key == "started_at" and not start_ok(value):
        problem = f"bad started_at {value}"
    elif key == "harness" and value.partition(":")[0] not in SLUGS.values():
        problem = f"unknown harness {value}"
    return problem


def check_blob(blob: str) -> str | None:
    """Why a trailer value's ``k=v;...`` attribute blob is invalid, or None."""
    pairs = blob.split(";")
    keys = [pair.partition("=")[0] for pair in pairs]
    problem = next(filter(None, map(check_pair, pairs)), None)
    missing = [key for key in REQUIRED if key not in keys]
    if problem is None and missing:
        problem = f"{missing[0]} required"
    elif problem is None and (keys != sorted(keys) or len(set(keys)) != len(keys)):
        problem = "attributes out of order or duplicated"
    return problem


def check_value(value: str) -> str | None:
    """Why a trailer value is invalid under the grammar, or None."""
    tokens = value.split()
    blobs = [t for t in tokens[1:] if "=" in t]
    if not tokens or tokens[0] != "LLM":
        return "must start with literal LLM"
    if len(blobs) != 1:
        return "need exactly one attribute blob"
    return check_blob(blobs[0])


def check_message(text: str) -> list[str]:
    """Every problem in a commit message's LLM trailers."""
    errs: list[str] = []
    keys_seen: list[str] = []
    for line in text.splitlines():
        match = LINE.match(line)
        if match:
            problem = check_value(match.group(2))
            if problem:
                errs.append(f"{line}: {problem}")
            if match.group(1) == KEY_GEN and KEY_ASSIST in keys_seen:
                errs.append(f"{line}: Generated-by must come before Assisted-by")
            keys_seen.append(match.group(1))
    return errs
