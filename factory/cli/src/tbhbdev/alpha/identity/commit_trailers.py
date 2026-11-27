# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Stamp and merge identity trailers in Git commit messages."""

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.sessions import load_record
from tbhbdev.alpha.identity.trailers import (
    KEY_ASSIST,
    KEY_GEN,
    replace_trailer_fields,
    start_ok,
    trailer_start,
)
from tbhbdev.alpha.identity.validation import (
    ATTR_PAIR,
    Trailer,
    check_value,
    llm_trailers,
)
from tbhbdev.alpha.identity.values import Ident, format_value, hash_ok
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from pathlib import Path

LLM_LINES = re.compile(r"^(?:Generated-by|Assisted-by):[ \t]*LLM\b.*\n?", re.MULTILINE)

AGENT_ATTR = re.compile(r"(?<![^\s;])(session|subagent)=([^\s;]+)")


def trailer_agent(value: str) -> tuple[str | None, str | None]:
    """(session, subagent) named by a trailer value; None where absent."""
    attrs = dict(AGENT_ATTR.findall(value))
    return attrs.get("session"), attrs.get("subagent")


def without_start(value: str) -> str:
    """A trailer value with its ``started_at`` attribute left out."""
    parts = value.split(";")
    return ";".join(p for p in parts if not p.startswith("started_at="))


def with_record(value: str, *, branch_start: str | None = None) -> str:
    """A trailer value with the ``launch`` and ``started_at`` of its session record.

    The record is the machine's word, so its fields replace whatever the line
    says. Invalid lines stay untouched for commit-msg to reject. Other text,
    including prose around the attribute blob, stays byte-for-byte as written.
    """
    if check_value(value):
        return value
    attrs = dict(ATTR_PAIR.findall(value))
    root, sub = hash_ok(attrs.get("session")), hash_ok(attrs.get("subagent"))
    if not root:
        return value
    root_rec = load_record(root)
    rec = load_record(sub, root=root) if sub else root_rec
    if not (rec or root_rec or branch_start):
        return value
    fields = {
        "launch": hash_ok(rec.get("launch")) or hash_ok(root_rec.get("launch")),
        "started_at": branch_start or start_ok(rec.get("started_at")),
    }
    return replace_trailer_fields(value, fields)


def earliest_starts(found: list[Trailer]) -> list[Trailer]:
    """Each trailer replaced by its twin with the earliest start.

    Twins have the same key and the same value apart from ``started_at``, so
    the hook's re-stamp of an amended commit collapses onto the original line.
    A line that differs in any other attribute stays apart, which means a
    hand-written early time cannot replace the hook's line and drop its
    ``launch``. The twin with the earliest ``started_at`` replaces the rest, so
    a value with the field beats one without it.
    """
    best: dict[tuple[str, str], str] = {}
    for key, value in found:
        twin = (key, without_start(value))
        start = trailer_start(value)
        if start and (twin not in best or start < trailer_start(best[twin])):
            best[twin] = value
    return [(k, best.get((k, without_start(v)), v)) for k, v in found]


def merge_trailers(found: list[Trailer]) -> list[Trailer]:
    """Deduplicated, ``Generated-by`` first; Generated-by beats Assisted-by.

    An agent with any ``Generated-by`` loses its ``Assisted-by`` lines, and
    one agent's lines merge into the one with the earliest ``started_at``.
    """
    generated = {trailer_agent(v) for k, v in found if k == KEY_GEN}
    kept = [
        (k, v)
        for k, v in earliest_starts(found)
        if k == KEY_GEN or trailer_agent(v) not in generated
    ]
    return sorted(dict.fromkeys(kept), key=lambda kv: kv[0] != KEY_GEN)


def with_start(ident: Ident) -> Ident:
    """An identity with the ``launch`` and ``started_at`` of its session record.

    A subagent's record has its own ``started_at``, and its ``launch`` falls
    back to its root's.
    """
    root_rec = load_record(ident.root)
    rec = load_record(ident.subagent, root=ident.root) if ident.subagent else root_rec
    return replace(
        ident,
        launch=hash_ok(rec.get("launch")) or hash_ok(root_rec.get("launch")),
        started_at=start_ok(rec.get("started_at")),
    )


def add_trailers(
    msgfile: Path,
    gen: list[Ident],
    assisted: list[Ident],
    kept: list[Trailer] | None = None,
    *,
    branch_start: str | None = None,
) -> None:
    """Rewrite the message's ``LLM`` trailers as one merged, ordered set.

    The set is the message's own ``LLM`` trailers, ``kept`` (an amended
    commit's) and the new ones. A new line replaces the older lines of its own
    agent, so nothing typed in a message overrides what the hook stamps, and a
    session record's ``launch`` and ``started_at`` replace any line's.
    A human's Signed-off-by (if any) is left where git put it.
    """
    text = msgfile.read_text()
    new = [(KEY_GEN, format_value(i.attrs())) for i in gen] + [
        (KEY_ASSIST, format_value(i.attrs())) for i in assisted
    ]
    made = {(k, trailer_agent(v)) for k, v in new}
    older = [
        (k, v)
        for k, v in [*llm_trailers(text), *(kept or [])]
        if (k, trailer_agent(v)) not in made
    ]

    def stamp_old(value: str) -> str:
        recorded = branch_start if trailer_start(value) == branch_start else None
        return with_record(value, branch_start=recorded)

    stamped = [(k, with_record(v, branch_start=branch_start)) for k, v in new]
    merged = merge_trailers([(k, stamp_old(v)) for k, v in older] + stamped)
    args = [f"--trailer={key}: {value}" for key, value in merged]
    bare = msgfile.with_name(msgfile.name + ".ascm")
    bare.write_text(LLM_LINES.sub("", text))
    code, out = git(
        "interpret-trailers", "--if-exists", "addIfDifferent", *args, str(bare)
    )
    bare.unlink()
    if code == 0:
        msgfile.write_text(out + "\n")


SQUASH_MARK = " This is a combination of"

COMMENT_CHAR_CONFIG = "core.commentChar"


def amend_only(path: Path, text: str, kept: list[Trailer]) -> None:
    """Rewrite the trailers of a message with no committer: keep, then merge.

    A message whose lines differ in no start time stays as written, so a human
    commit is never reordered.
    """
    found = llm_trailers(text)
    # A squash joins the messages, so each one's lines sit in its own block.
    _, comment = git("config", COMMENT_CHAR_CONFIG)
    # Git chooses auto from this candidate list after inspecting the message.
    markers = "#;@!$%^&|:" if comment == "auto" else (comment or "#")
    joined = bool(found) and re.search(
        rf"^[{re.escape(markers)}]{re.escape(SQUASH_MARK)}", text, re.MULTILINE
    )
    if kept or joined or earliest_starts(found) != found:
        add_trailers(path, [], [], kept)
