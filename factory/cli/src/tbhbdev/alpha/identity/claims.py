# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Track which staged edits a successful commit can consume."""

from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.identity.edits import EDITS, blob_of
from tbhbdev.alpha.identity.storage import (
    Repo,
    locked,
    read_record,
    repo_info,
    write_json,
)
from tbhbdev.alpha.identity.values import hx
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from tbhbdev.alpha.identity.payload import Json

CLAIMS = "claims"


def staged_info(cwd: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """(staged paths, path -> staged blob hash)."""
    _, out = git("diff", "--cached", "--name-only", "-z", cwd=cwd)
    paths = [p for p in out.split("\0") if p]  # empty output when git fails
    blobs: dict[str, str] = {}
    if paths:
        _, out = git("ls-files", "-s", "-z", "--", *paths, cwd=cwd)
        for entry in out.split("\0"):
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[2] == "0":  # noqa: PLR2004 -- mode, blob, stage
                blobs[path] = parts[1]
    return paths, blobs


def unstaged_blobs(top: Path, staged: set[str]) -> dict[str, str]:
    """The work-tree blob of each staged file whose work tree differs from the index."""
    _, out = git("diff", "--name-only", "-z", cwd=top)
    found = ((p, blob_of(top, p)) for p in out.split("\0") if p in staged)
    return {path: blob for path, blob in found if blob}


def committing(
    edits: list[tuple[Path, Json]], staged: set[str], unstaged: Mapping[str, str]
) -> list[tuple[Path, Json]]:
    """The receipts this commit contains: for a staged path, and not still unstaged.

    A receipt whose blob is exactly the path's unstaged work-tree content records
    an edit made after the path was staged. That edit is not in this commit, so
    it waits for the commit that stages it.
    """
    return [
        (path, rec)
        for path, rec in edits
        if rec["path"] in staged
        and not (rec["path"] in unstaged and rec.get("blob") == unstaged[rec["path"]])
    ]


def write_claim(repo: Repo, root: str, receipts: list[Path]) -> None:
    """Record the receipts the trailers credit, keyed by the tree being committed.

    Post-commit consumes them once a commit of exactly that tree exists. A
    failed attempt leaves only a claim that a retry of the same tree rewrites.
    """
    code, tree = git("write-tree", cwd=repo.top)
    if code == 0:
        write_json(
            repo.state / CLAIMS / f"{tree}.json",
            {"root": root, "receipts": sorted(p.name for p in receipts)},
        )


def cmd_consume() -> int:
    """post-commit: consume the receipts HEAD's trailers credited (its claim).

    Only post-commit consumes, so nothing is consumed unless a commit exists.
    """
    repo = repo_info()
    if not repo:
        return 0
    _, tree = git("rev-parse", "HEAD^{tree}", cwd=repo.top)
    claim_path = repo.state / CLAIMS / f"{tree}.json"
    with locked(repo.state):
        claim = read_record(claim_path)
        if not claim:
            return 0
        folder = repo.state / EDITS / hx(str(claim["root"]))
        for name in cast("list[str]", claim["receipts"]):
            rec = read_record(folder / name)
            if rec:
                rec["consumed"] = True
                write_json(folder / name, rec)
        claim_path.unlink()
    return 0
