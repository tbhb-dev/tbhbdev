# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity commit picking."""

from __future__ import annotations

import pytest
from unit.alpha.identity.identity_examples import (
    ASSIST_OTHER,
    SHA_A,
    SHA_B,
    SRC_GEN,
    SRC_GEN_NO_MODEL,
)

from tbhbdev.alpha.identity import trailers
from tbhbdev.alpha.identity.commit import picking as commit_picking


@pytest.mark.parametrize(
    ("head", "text", "expected"),
    [
        (None, "msg\n", None),
        (SHA_B, "msg\n", SHA_B),
        (SHA_B, f"msg\n\n(cherry picked from commit {SHA_A})\n", SHA_B),
        (None, f"msg\n\n(cherry picked from commit {SHA_A})\n", SHA_A),
        (
            None,
            (
                f"(cherry picked from commit {SHA_A})\n\n"
                f"(cherry picked from commit {SHA_B})\n"
            ),
            SHA_B,
        ),
        (None, f"(cherry picked from commit {SHA_A[:39]})\n", None),
        (None, f"(cherry picked from commit {SHA_A.upper()})\n", None),
        (None, f"see (cherry picked from commit {SHA_A})\n", None),
        (None, f"(cherry picked from commit {SHA_A}) x\n", None),
        (
            None,
            f"(cherry picked from commit {SHA_A})\n(cherry picked from commit xyz)\n",
            SHA_A,
        ),
    ],
)
def test_pick_source(head: str | None, text: str, expected: str | None) -> None:
    assert commit_picking.pick_source(head, text) == expected


@pytest.mark.parametrize(
    ("trailers_", "own", "expected"),
    [
        ([], True, True),
        ([], False, False),
        ([(trailers.KEY_GEN, SRC_GEN)], True, False),
        ([(trailers.KEY_GEN, SRC_GEN)], False, False),
        ([(trailers.KEY_GEN, SRC_GEN_NO_MODEL)], True, True),
        ([(trailers.KEY_GEN, SRC_GEN_NO_MODEL)], False, False),
        ([(trailers.KEY_ASSIST, SRC_GEN)], True, True),
        ([(trailers.KEY_GEN, "LLM harness=x;not-model=y")], True, True),
        ([(trailers.KEY_GEN, "LLM harness=x;model=")], True, True),
        (
            [(trailers.KEY_GEN, SRC_GEN_NO_MODEL), (trailers.KEY_GEN, SRC_GEN)],
            True,
            False,
        ),
    ],
)
def test_pick_refused(
    trailers_: list[trailers.Trailer], own: bool, expected: bool
) -> None:
    assert commit_picking.pick_refused(trailers_, own=own) is expected


def test_usable_trailers_drops_a_generated_by_without_a_model() -> None:
    gen = (trailers.KEY_GEN, SRC_GEN)
    bare = (trailers.KEY_GEN, SRC_GEN_NO_MODEL)
    assert commit_picking.usable_trailers([bare, gen, ASSIST_OTHER]) == [
        gen,
        ASSIST_OTHER,
    ]
    assert commit_picking.usable_trailers([]) == []
