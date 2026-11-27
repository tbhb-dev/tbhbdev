# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request description."""

from hypothesis import given
from hypothesis import strategies as st
from unit.alpha.pull_request.pr_draft_samples import (
    BASE,
    FALLBACK,
    GEN,
    OTHER,
    SESSION,
    SUBAGENT,
    structure,
)

from tbhbdev.alpha.identity.commit.grammar import check_message
from tbhbdev.alpha.identity.trailers import (
    format_value,
    identity_key,
    merge_pr_trailers,
)
from tbhbdev.alpha.pull_request import body as pr_body
from tbhbdev.alpha.pull_request import description as pr_description


def value(**attrs: str) -> str:
    return format_value(attrs)


def test_merge_dedupes_and_orders() -> None:
    a = value(harness="claude-code", model="a:m1")
    b = value(harness="codex", model="a:m2")
    found = [("Assisted-by", a), ("Generated-by", b), ("Assisted-by", a)]
    assert merge_pr_trailers(found, None) == [("Generated-by", b), ("Assisted-by", a)]


def test_generated_by_wins_whichever_comes_first() -> None:
    low = value(effort="low", harness="claude-code", model="a:m")
    high = value(effort="high", harness="claude-code", model="a:m")
    assert merge_pr_trailers([("Assisted-by", low), ("Generated-by", high)], None) == [
        ("Generated-by", high)
    ]
    assert merge_pr_trailers([("Generated-by", high), ("Assisted-by", low)], None) == [
        ("Generated-by", high)
    ]
    assert merge_pr_trailers([("Generated-by", high), ("Generated-by", low)], None) == [
        ("Generated-by", high)
    ]
    assert merge_pr_trailers([("Assisted-by", high), ("Assisted-by", low)], None) == [
        ("Assisted-by", high)
    ]


def test_posting_agent_is_added_as_assisted_once() -> None:
    a = value(harness="claude-code", model="a:m")
    me = value(harness="codex", model="o:m")
    assert merge_pr_trailers([("Generated-by", a)], me) == [
        ("Generated-by", a),
        ("Assisted-by", me),
    ]
    assert merge_pr_trailers([], me) == [("Assisted-by", me)]
    assert merge_pr_trailers([("Generated-by", me)], me) == [("Generated-by", me)]
    assert merge_pr_trailers([("Assisted-by", me)], me) == [("Assisted-by", me)]
    assert merge_pr_trailers([], None) == []


def identity(harness: str, model: str, session: str) -> str:
    extra = {"session": session} if session else {}
    return value(harness=harness, model=model, **extra)


IDENTITIES = st.builds(
    identity,
    st.sampled_from(["claude-code", "codex"]),
    st.sampled_from(["a:m1", "a:m2"]),
    st.sampled_from(["", SESSION, SUBAGENT]),
)


TRAILERS = st.lists(
    st.tuples(st.sampled_from(["Generated-by", "Assisted-by"]), IDENTITIES)
)


@given(TRAILERS, st.none() | IDENTITIES)
def test_merge_properties(found: list[tuple[str, str]], posting: str | None) -> None:
    merged = merge_pr_trailers(found, posting)
    keys = [identity_key(v) for _, v in merged]
    assert len(keys) == len(set(keys))
    wanted = {identity_key(v) for _, v in found}
    if posting is not None:
        wanted.add(identity_key(posting))
    assert set(keys) == wanted
    roles = [k for k, _ in merged]
    assert roles == sorted(roles, key=lambda role: role != "Generated-by")
    generated = {identity_key(v) for k, v in found if k == "Generated-by"}
    for (role, _), key in zip(merged, keys, strict=True):
        assert (role == "Generated-by") == (key in generated)
    assert not check_message("\n".join(f"{k}: {v}" for k, v in merged))


def test_body_of_keeps_spaces_and_edge_letters() -> None:
    assert pr_description.body_of(["# T", "", "## Why", "x  ", ""]) == "## Why\nx  \n"
    assert pr_description.body_of(["# T", "", "Xylophone X", ""]) == "Xylophone X\n"
    assert pr_description.body_of(["# T", "## Why", "x"]) == "## Why\nx\n"


def test_same_identity_in_commit_and_posting_is_one_generated_by() -> None:
    """Generated-by wins over the posting agent's own Assisted-by."""
    assert merge_pr_trailers([("Generated-by", FALLBACK)], FALLBACK) == [
        ("Generated-by", FALLBACK)
    ]


def test_generated_by_precedes_assisted_by() -> None:
    assert merge_pr_trailers([("Assisted-by", OTHER), ("Generated-by", GEN)], None) == [
        ("Generated-by", GEN),
        ("Assisted-by", OTHER),
    ]


def test_an_opened_by_line_does_not_hide_later_trailers() -> None:
    assert structure(BASE + "\nOpened-by: x\nGenerated-by: LLM x\n") == [
        (15, pr_body.OPENED_MESSAGE),
        (16, "the script writes Generated-by and Assisted-by; remove this line"),
    ]
