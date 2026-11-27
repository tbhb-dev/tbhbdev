# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Select stampable Markdown documents from the staged diff."""

from __future__ import annotations

from pathlib import PurePosixPath

BARE_DOCS = {"AGENTS.md", "CLAUDE.md", "index.md", "log.md"}


TESTDATA = "testdata"


def stampable_docs(diff: str) -> list[tuple[str, str, str]]:
    """The stampable document paths, modes and blobs in a raw Git diff."""
    fields = diff.split("\0")
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
    return docs
