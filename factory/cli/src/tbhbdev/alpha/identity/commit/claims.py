# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Claim edit receipts for a staged tree and consume them after commit."""

from __future__ import annotations

from typing import cast
lazy from pathlib import Path

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import storage
from tbhbdev.alpha.identity.receipts import EDITS
from tbhbdev.alpha.identity.records import hx
from tbhbdev.alpha.identity.storage import Repo, locked, write_json

CLAIMS = "claims"


def write_claim(repo: Repo, root: str, receipts: list[Path]) -> None:
    """Record the receipts the trailers credit, keyed by the tree being committed.

    Post-commit consumes them once a commit of exactly that tree exists. A
    failed attempt leaves only a claim that a retry of the same tree rewrites.
    """
    code, tree = git_repo.git("write-tree", cwd=repo.top)
    if code == 0:
        write_json(
            repo.state / CLAIMS / f"{tree}.json",
            {"root": root, "receipts": sorted(p.name for p in receipts)},
        )


def cmd_consume() -> int:
    """post-commit: consume the receipts HEAD's trailers credited (its claim).

    Only post-commit consumes, so nothing is consumed unless a commit exists.
    """
    repo = storage.repo_info()
    if not repo:
        return 0
    _, tree = git_repo.git("rev-parse", "HEAD^{tree}", cwd=repo.top)
    claim_path = repo.state / CLAIMS / f"{tree}.json"
    with locked(repo.state):
        claim = storage.read_record(claim_path)
        if not claim:
            return 0
        folder = repo.state / EDITS / hx(str(claim["root"]))
        for name in cast("list[str]", claim["receipts"]):
            rec = storage.read_record(folder / name)
            if rec:
                rec["consumed"] = True
                write_json(folder / name, rec)
        claim_path.unlink()
    return 0
