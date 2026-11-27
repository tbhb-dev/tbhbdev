# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Decide which attribution a cherry-pick keeps or requires."""

from __future__ import annotations

import re

from tbhbdev.alpha.identity.trailers import KEY_GEN, Trailer, llm_trailers

PICKED_FROM = re.compile(
    r"^\(cherry picked from commit ([0-9a-f]{40})\)$", re.MULTILINE
)


MODEL_ATTR = re.compile(r"(?<![^\s;])model=[^\s;]")


def pick_source(head: str | None, text: str) -> str | None:
    """The picked commit: ``CHERRY_PICK_HEAD``, else the last ``-x`` line."""
    found = PICKED_FROM.findall(text)
    return head or (found[-1] if found else None)


def pick_refused(src_trailers: list[Trailer], *, own: bool) -> bool:
    """Whether to refuse a pick: our own agent's commit with no model named."""
    return own and not any(
        key == KEY_GEN and MODEL_ATTR.search(value) for key, value in src_trailers
    )


def usable_trailers(src_trailers: list[Trailer]) -> list[Trailer]:
    """The trailers worth keeping; commit-msg rejects a ``Generated-by`` sans model."""
    return [
        (key, value)
        for key, value in src_trailers
        if key != KEY_GEN or MODEL_ATTR.search(value)
    ]


def cherry_pick_problem(
    sha: str, src_trailers: list[Trailer], text: str, *, own: bool
) -> str | None:
    """Explain a refusal when an agent-authored source has no model trailer."""
    present = [*llm_trailers(text), *src_trailers]
    if not pick_refused(present, own=own):
        return None
    short = sha[:7]
    return (
        f"cherry-pick of {short} has no Generated-by trailer (with model=); "
        f"add the source's trailer by hand (git log -1 --format=%B {short}) "
        "and commit again"
    )
