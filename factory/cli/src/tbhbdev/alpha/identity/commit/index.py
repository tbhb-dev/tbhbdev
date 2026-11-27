# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Parse the Git index snapshot used for attribution."""

from __future__ import annotations


def index_blobs(output: str) -> dict[str, str]:
    """Stage-zero Git index entries keyed by path."""
    blobs = {}
    for entry in output.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[2] == "0":  # noqa: PLR2004 -- mode, blob, stage
            blobs[path] = parts[1]
    return blobs
