# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read and write commit trailers and their recorded start times."""

from __future__ import annotations

lazy from pathlib import Path

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity.commit.formatting import (
    apply_record,
    identity_with_start,
    should_merge_message,
    trailer_candidates,
)
from tbhbdev.alpha.identity.commit.grammar import check_value
from tbhbdev.alpha.identity.records import hash_ok
from tbhbdev.alpha.identity.sessions import load_record
from tbhbdev.alpha.identity.trailers import (
    ATTR_PAIR,
    LLM_LINES,
    Trailer,
    merge_trailers,
    trailer_start,
)
lazy from tbhbdev.alpha.identity.models import Ident


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
    return apply_record(value, root_rec, rec, branch_start)


def with_start(ident: Ident) -> Ident:
    """An identity with the ``launch`` and ``started_at`` of its session record.

    A subagent's record has its own ``started_at``, and its ``launch`` falls
    back to its root's.
    """
    root_rec = load_record(ident.root)
    rec = load_record(ident.subagent, root=ident.root) if ident.subagent else root_rec
    return identity_with_start(ident, root_rec, rec)


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
    older, new = trailer_candidates(text, gen, assisted, kept or [])

    def stamp_old(value: str) -> str:
        recorded = branch_start if trailer_start(value) == branch_start else None
        return with_record(value, branch_start=recorded)

    stamped = [(k, with_record(v, branch_start=branch_start)) for k, v in new]
    merged = merge_trailers([(k, stamp_old(v)) for k, v in older] + stamped)
    args = [f"--trailer={key}: {value}" for key, value in merged]
    bare = msgfile.with_name(msgfile.name + ".ascm")
    bare.write_text(LLM_LINES.sub("", text))
    code, out = git_repo.git(
        "interpret-trailers", "--if-exists", "addIfDifferent", *args, str(bare)
    )
    bare.unlink()
    if code == 0:
        msgfile.write_text(out + "\n")


COMMENT_CHAR_CONFIG = "core.commentChar"


def amend_only(path: Path, text: str, kept: list[Trailer]) -> None:
    """Rewrite the trailers of a message with no committer: keep, then merge.

    A message whose lines differ in no start time stays as written, so a human
    commit is never reordered.
    """
    _, comment = git_repo.git("config", COMMENT_CHAR_CONFIG)
    if should_merge_message(text, kept, comment):
        add_trailers(path, [], [], kept)
