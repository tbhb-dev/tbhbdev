# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Capture edits against Git worktrees and load their receipts."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import storage
from tbhbdev.alpha.identity.records import Json, hx
from tbhbdev.alpha.identity.storage import Repo, write_json
lazy from tbhbdev.alpha.identity.models import Ident

EDITS = "edits"


def rel_to_repo(path: object, base: Path | None = None) -> tuple[str, Repo] | None:
    """(repo-relative path, repo) of a file, or None when outside any repo."""
    if not isinstance(path, str) or not path:
        return None
    target = Path(path)
    if not target.is_absolute():
        target = (base or Path.cwd()) / target
    existing = next(p for p in target.parents if p.is_dir())
    repo = storage.repo_info(existing)
    if not repo:
        return None
    rel = Path(
        os.path.relpath(existing.resolve() / target.relative_to(existing), repo.top)
    )
    return None if rel.parts[:1] == ("..",) else (rel.as_posix(), repo)


def blob_of(top: Path, rel: str, *, store: bool = False) -> str | None:
    """The git blob hash of a work-tree file, or None if it is not a file.

    ``store`` also writes the blob into the object database (``-w``).
    """
    path = top / rel
    if not path.is_file():
        return None
    flags = ("-w",) if store else ()
    code, out = git_repo.git("hash-object", *flags, str(path), cwd=top)  # absolute path
    return out if code == 0 else None


def write_edit(
    ident: Ident,
    path: object,
    base: Path | None = None,
    *,
    store: bool = False,
) -> None:
    """Store one edit receipt for ``path``; nothing outside a repository.

    ``store`` also writes the blob into the object database.
    """
    found = rel_to_repo(path, base)
    if not found:
        return
    rel, repo = found
    receipt: Json = {
        "root": ident.root,
        "subagent": ident.subagent,
        "harness": ident.harness,
        "model": ident.model,
        "effort": ident.effort,
        "surface": ident.surface,
        "path": rel,
        "blob": blob_of(repo.top, rel, store=store),
        "consumed": False,
        "ts": time.time(),
    }
    write_json(
        repo.state / EDITS / hx(ident.root) / f"{uuid.uuid4().hex}.json", receipt
    )


def load_edits(repo: Repo, root: str) -> list[tuple[Path, Json]]:
    """Unconsumed edit receipts for a root session."""
    out = []
    for path in (repo.state / EDITS / hx(root)).glob("*.json"):
        rec = storage.read_record(path)
        if rec and not rec.get("consumed"):
            out.append((path, rec))
    return out


def all_edits(repo: Repo) -> list[Json]:
    """Every unconsumed edit receipt in the repository, whatever the root session."""
    receipts = (storage.read_record(p) for p in (repo.state / EDITS).glob("*/*.json"))
    return [rec for rec in receipts if rec and not rec.get("consumed")]
