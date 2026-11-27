# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read Git index and session data for commit attribution."""

from __future__ import annotations

lazy from pathlib import Path

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity.claude.logs import transcript_model
from tbhbdev.alpha.identity.commit.index import index_blobs
from tbhbdev.alpha.identity.commit.roles import enrich_ident
from tbhbdev.alpha.identity.models import CLAUDE, Committer, Ident
from tbhbdev.alpha.identity.receipts import blob_of
from tbhbdev.alpha.identity.sessions import load_record


def staged_info(cwd: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """(staged paths, path -> staged blob hash)."""
    _, out = git_repo.git("diff", "--cached", "--name-only", "-z", cwd=cwd)
    paths = [p for p in out.split("\0") if p]  # empty output when git fails
    blobs: dict[str, str] = {}
    if paths:
        _, out = git_repo.git("ls-files", "-s", "-z", "--", *paths, cwd=cwd)
        blobs = index_blobs(out)
    return paths, blobs


def enrich(ident: Ident, committer: Committer) -> Ident:
    """Fill what the edit-time hook could not see.

    The harness version (the hook env lacks ``CLAUDE_CODE_EXECPATH``) comes from
    the committer of the same harness. A Claude child's model and effort come
    from its record, because the child transcript can trail the first
    PostToolUse. Surface is per session, and an edit hook env may not expose it
    (agy). A main agent's identity stored without a model or effort takes them
    from the committer's root identity when that is the same agent. Never
    inherits a model from another agent.
    """
    child_rec = (
        load_record(ident.subagent)
        if ident.subagent
        and committer.key == CLAUDE
        and not (ident.model and ident.effort)
        else {}
    )
    child_model = (
        child_rec.get("model") or transcript_model(child_rec.get("transcript"))
        if child_rec.get("root") == ident.root
        else None
    )
    return enrich_ident(
        ident,
        committer,
        child_model,
        child_rec.get("effort") if child_rec.get("root") == ident.root else None,
    )


def unstaged_blobs(top: Path, staged: set[str]) -> dict[str, str]:
    """The work-tree blob of each staged file whose work tree differs from the index."""
    _, out = git_repo.git("diff", "--name-only", "-z", cwd=top)
    found = ((p, blob_of(top, p)) for p in out.split("\0") if p in staged)
    return {path: blob for path, blob in found if blob}
