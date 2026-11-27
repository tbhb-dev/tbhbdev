# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Parse LLM trailers and validate commit attribution and Git identity."""

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.models import EFFORTS
from tbhbdev.alpha.identity.trailers import KEY_ASSIST, KEY_GEN, start_ok, trailer_start
from tbhbdev.alpha.identity.values import ATTR_ORDER, HASH_RE, PROG, SLUGS, detect
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from collections.abc import Mapping

# Attributes every trailer value needs, in the order a missing one is reported.
REQUIRED = ("harness", "model")

LLM_LINE = re.compile(r"^(Generated-by|Assisted-by):[ \t]*(LLM\b.*)$", re.MULTILINE)

ATTR_PAIR = re.compile(r"(?<![^\s;])([a-z_]+)=([^\s;]+)")

IDENT_EMAIL = re.compile(r"<([^>]*)>")


type Trailer = tuple[str, str]


def llm_trailers(text: str) -> list[Trailer]:
    """The (key, value) of each ``LLM`` trailer line in a message."""
    return [(m[1], m[2]) for m in LLM_LINE.finditer(text)]


def ident_email(ident: str) -> str:
    """The address in a ``Name <email> date`` identity; empty when there is none."""
    found = IDENT_EMAIL.search(ident)
    return found[1] if found else ""


LINE = re.compile(r"^(Generated-by|Assisted-by):\s*(.*)$")

# The one identity every agent commit is authored and committed as.
AGENT_IDENT = "tbhb.dev Agent <agent@tonyburns.net>"

AGENT_EMAIL = AGENT_IDENT.partition("<")[2].removesuffix(">")

SESSION_ERROR = (
    "this commit claims the agent identity but no agent session is attached. "
    "Agents: commit from the harness's own host, where the session's hooks run. "
    "Humans: commit as yourself; the agent identity is reserved for agent sessions."
)

# The timestamp and zone at the end of ``git var GIT_*_IDENT``.
IDENT_DATE = re.compile(r" [0-9]+ [-+][0-9]{4}$")

# A sign-off or co-author line; git matches trailer keys without regard to case.
SIGNOFF = re.compile(
    r"^(?:Signed-off-by|Co-authored-by)[ \t]*:", re.IGNORECASE | re.MULTILINE
)

SIGNOFF_ERROR = (
    "agent commits carry no Signed-off-by or Co-authored-by; the operator signs off"
)

START_WARNING = "a Generated-by line lacks started_at"

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


def ident_problems(text: str, env: Mapping[str, str]) -> list[str]:
    """Why a commit fails the agent identity's session or attribution rules.

    Every agent commit is authored and committed as ``AGENT_IDENT``, and
    ``text`` carries no ``Signed-off-by`` or ``Co-authored-by`` line, whoever it
    names; the operator signs off. ``git var`` gives the identities this commit
    records, including ``--author``, the author ``--amend`` keeps, and ``GIT_*``
    overrides. Outside a harness, the agent email requires a Generated-by
    trailer; human identities aren't checked, even when crediting agent edits.
    """
    if detect(env) is None:
        if any(line.startswith(f"{KEY_GEN}:") for line in text.splitlines()):
            return []
        for role in ("AUTHOR", "COMMITTER"):
            _, out = git("var", f"GIT_{role}_IDENT")
            if IDENT_DATE.sub("", out).endswith(f"<{AGENT_EMAIL}>"):
                return [SESSION_ERROR]
        return []
    errs = []
    for role in ("author", "committer"):
        _, out = git("var", f"GIT_{role.upper()}_IDENT")
        who = IDENT_DATE.sub("", out)
        if who != AGENT_IDENT:
            errs.append(f"{role} is {who!r}; an agent commit must use {AGENT_IDENT}")
    if SIGNOFF.search(text):
        errs.append(SIGNOFF_ERROR)
    return errs


def cmd_check(msgfile: str, env: Mapping[str, str]) -> int:
    """commit-msg: validate the LLM trailers and an agent commit's identity."""
    text = Path(msgfile).read_text()
    errs = check_message(text) + ident_problems(text, env)
    for err in errs:
        print(err if err == SESSION_ERROR else f"{PROG}: {err}", file=sys.stderr)
    if detect(env) and any(
        k == KEY_GEN and not trailer_start(v) for k, v in llm_trailers(text)
    ):
        print(f"{PROG}: warning: {START_WARNING}", file=sys.stderr)
    return 1 if errs else 0
