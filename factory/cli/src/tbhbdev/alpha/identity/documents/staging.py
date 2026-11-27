# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read staged documents and write their stamped contents back to Git."""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
lazy from pathlib import Path

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity.diagnostics import PROG
from tbhbdev.alpha.identity.documents.frontmatter import Entry, stamp_text
from tbhbdev.alpha.identity.documents.selection import stampable_docs

MERGE_HEAD = "MERGE_HEAD"


def git_raw(*args: str, cwd: Path, data: bytes | None = None) -> tuple[int, bytes]:
    """Run Git with raw input and output."""
    result = subprocess.run(  # nosec B603 B607
        ["git", *args], capture_output=True, cwd=cwd, input=data, check=False
    )
    return result.returncode, result.stdout


def staged_docs(top: Path) -> list[tuple[str, str, str]]:
    """(path, mode, blob) of each staged added or modified stampable document."""
    _, raw = git_raw(
        "diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames",
        "--diff-filter=AM", cwd=top,
    )  # fmt: skip
    docs = stampable_docs(os.fsdecode(raw))
    heads = merge_heads(top)
    return not_from_merge(top, docs, heads) if heads else docs


def merge_heads(top: Path) -> list[str]:
    """The commits a merge brings in, one per line of MERGE_HEAD; none outside a merge.

    Git resolves a branch named `MERGE_HEAD` too. Read the merge state file
    directly so the caller can distinguish a single parent from several.
    """
    _, path = git_repo.git("rev-parse", "--git-path", MERGE_HEAD, cwd=top)
    try:
        return (top / path).read_text().split()
    except OSError:
        return []


def not_from_merge(
    top: Path, docs: list[tuple[str, str, str]], heads: list[str]
) -> list[tuple[str, str, str]]:
    """The staged documents whose content differs from every incoming parent."""
    paths = {path for path, _, _ in docs}
    for head in heads:
        code, raw = git_raw(
            "diff", "--cached", "--raw", "-z", "--no-renames", head, cwd=top
        )
        if code != 0:
            print(
                f"{PROG}: can't compare with {head}; stamping every staged document",
                file=sys.stderr,
            )
            return docs
        changed = {os.fsdecode(path) for path in raw.split(b"\0")[1::2]}
        paths &= changed
    return [doc for doc in docs if doc[0] in paths]


def stamp_document(top: Path, doc: tuple[str, str, str], entries: list[Entry]) -> None:
    """Stamp the staged blob, then the work-tree file, with the same rewrite.

    Both change together so prek sees the index and work tree still agree.
    """
    rel, mode, blob = doc
    _, raw = git_raw("cat-file", "blob", blob, cwd=top)
    text = raw.decode()
    stamped = stamp_text(text, entries)
    if stamped == text:
        return
    _, output = git_raw("hash-object", "-w", "--stdin", cwd=top, data=stamped.encode())
    new_blob = output.decode().strip()
    git_repo.git("update-index", "--cacheinfo", f"{mode},{new_blob},{rel}", cwd=top)
    path = top / rel
    if path.is_file():
        current = path.read_bytes().decode()
        restamped = stamp_text(current, entries)
        if restamped != current:
            path.write_bytes(restamped.encode())
    print(f"{PROG}: stamped {rel}", file=sys.stderr)
