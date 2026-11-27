# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Stamp attribution into staged Markdown documents and their worktree files."""

import subprocess  # nosec B404
import sys
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.attribution import committer_identity
from tbhbdev.alpha.identity.codex_scan import refresh_codex
from tbhbdev.alpha.identity.contributors import stamp_entries
from tbhbdev.alpha.identity.edits import all_edits
from tbhbdev.alpha.identity.frontmatter import Entry, stamp_text
from tbhbdev.alpha.identity.storage import repo_info
from tbhbdev.alpha.identity.values import PROG
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from collections.abc import Mapping

# Reserved document names carry no `generated` list: AGENTS.md and CLAUDE.md are
# read verbatim by the harnesses, index.md and log.md follow the OKF index rules.
BARE_DOCS = {"AGENTS.md", "CLAUDE.md", "index.md", "log.md"}

# Fixtures, not documents: skipped like the docs checker (and Go's tooling) does.
TESTDATA = "testdata"

# The file git keeps while a merge waits for its commit.
MERGE_HEAD = "MERGE_HEAD"


def git_bytes(*args: str, cwd: Path, data: bytes | None = None) -> tuple[int, bytes]:
    """Run git; return its exit code and raw stdout without text conversion."""
    done = subprocess.run(  # nosec B603 B607
        ["git", *args], capture_output=True, cwd=cwd, input=data, check=False
    )
    return done.returncode, done.stdout


def staged_docs(top: Path) -> list[tuple[str, str, str]]:
    """(path, mode, blob) of each staged added or modified stampable document."""
    _, raw = git_bytes(
        "diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames",
        "--diff-filter=AM", cwd=top,
    )  # fmt: skip
    fields = decode(raw).split("\0")
    docs = []
    for meta, path in zip(fields[::2], fields[1::2], strict=False):  # pragma: no mutate
        pure = PurePosixPath(path)
        if (
            pure.suffix.lower() == ".md"
            and len(pure.parts) > 1
            and pure.name not in BARE_DOCS
            and TESTDATA not in pure.parts
        ):
            modes = meta.split()
            docs.append((path, modes[1], modes[3]))
    return not_from_merge(top, docs, merge_heads(top))


def merge_heads(top: Path) -> list[str]:
    """The commits a merge brings in, one per line of MERGE_HEAD; none outside a merge.

    Reads the file, because `rev-parse MERGE_HEAD` also matches a branch of that
    name and `rev-list` reads only the first parent of an octopus merge.
    """
    _, path = git("rev-parse", "--git-path", MERGE_HEAD, cwd=top)
    try:
        return (top / path).read_text().split()
    except OSError:
        return []


def not_from_merge(
    top: Path, docs: list[tuple[str, str, str]], heads: list[str]
) -> list[tuple[str, str, str]]:
    """The documents the merge commit itself changes.

    A file that matches any merged-in parent came from that parent as
    committed, so stamping it would rewrite someone else's provenance. When git
    can't compare, every document stays in, as outside a merge.
    """
    changed = []
    for head in heads:
        code, raw = git_bytes(
            "diff", "--cached", "--raw", "-z", "--no-renames", head, cwd=top
        )
        if code != 0:
            print(
                f"{PROG}: can't compare with {head}; stamping every staged document",
                file=sys.stderr,
            )
            return docs
        changed.append({decode(path) for path in raw.split(b"\0")[1::2]})
    return [doc for doc in docs if all(doc[0] in paths for paths in changed)]


def decode(raw: bytes) -> str:
    """Text that encodes back to exactly ``raw``, whatever bytes it holds."""
    return raw.decode(errors="surrogateescape")


def stamp_document(top: Path, doc: tuple[str, str, str], entries: list[Entry]) -> None:
    """Stamp the staged blob, then the work-tree file, with the same rewrite.

    Both change together so prek sees the index and work tree still agree.
    """
    rel, mode, blob = doc
    _, raw = git_bytes("cat-file", "blob", blob, cwd=top)
    text = decode(raw)
    stamped = stamp_text(text, entries)
    if stamped == text:
        return
    _, raw_blob = git_bytes(
        "hash-object",
        "-w",
        "--stdin",
        cwd=top,
        data=stamped.encode(errors="surrogateescape"),
    )
    new_blob = raw_blob.decode().strip()
    git("update-index", "--cacheinfo", f"{mode},{new_blob},{rel}", cwd=top)
    path = top / rel
    if path.is_file():
        current = decode(path.read_bytes())
        restamped = stamp_text(current, entries)
        if restamped != current:
            path.write_bytes(restamped.encode(errors="surrogateescape"))
    print(f"{PROG}: stamped {rel}", file=sys.stderr)


def cmd_stamp_docs(env: Mapping[str, str]) -> int:
    """pre-commit: stamp ``generated`` into the frontmatter of staged documents.

    Receipts are read, not consumed (post-commit consumes them).
    """
    repo = repo_info()
    if not repo:
        return 0
    committer = committer_identity(env)
    if committer:
        refresh_codex(committer, repo, env)
    receipts = all_edits(repo)
    now = time.time()
    # No cwd=None mutant matters below: the process already runs inside the repository.
    for doc in staged_docs(repo.top):  # pragma: no mutate
        mine = [r for r in receipts if r["path"] == doc[0]]
        entries = stamp_entries(mine, committer, now, repo.top)  # pragma: no mutate
        stamp_document(repo.top, doc, entries)
    return 0
