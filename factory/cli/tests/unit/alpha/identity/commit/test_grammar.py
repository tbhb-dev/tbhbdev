# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity commit grammar."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from unit.alpha.identity.identity_examples import (
    CHILD,
    CODEX_TRAILER_ATTRS,
    EARLY,
    GOOD,
    LAUNCH,
    ROOT,
    SESSION_HASH,
)

from tbhbdev.alpha.identity import trailers
from tbhbdev.alpha.identity.commit import grammar as commit_grammar
from tbhbdev.alpha.identity.models import EFFORTS

# Expected results from the rules trailer grammar at 4b48e96.
TRAILER_VALUE_CASES = [
    ("LLM harness=claude-code;model=anthropic:m", None),
    (
        (
            "LLM effort=max;harness=codex:1;model=openai:gpt-5;"
            f"session={ROOT};subagent={CHILD};surface=cli"
        ),
        None,
    ),
    (f"LLM {GOOD}", None),
    ("LLM harness=antigravity;model=gcp.gemini:m;surface=cli", None),
    ("LLM harness=codex;model=openai:m some prose", None),
    ("LLM prose harness=codex:1:2;model=a=b;surface=c=d more prose", None),
    *[
        (f"LLM effort={effort};harness=codex;model=m", None)
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    ],
    ("", "must start with literal LLM"),
    ("harness=codex", "must start with literal LLM"),
    ("llm harness=codex", "must start with literal LLM"),
    ("LLM", "need exactly one attribute blob"),
    ("LLM a=b c=d", "need exactly one attribute blob"),
    ("LLM harness=codex;", "bad attribute ''"),
    ("LLM harness=codex;Bad=1", "bad attribute 'Bad=1'"),
    ("LLM harness=codex;color=red", "unknown attribute color"),
    ("LLM effort=huge;harness=codex", "bad effort huge"),
    ("LLM harness=codex;session=nope", "bad session hash"),
    (f"LLM harness=codex;session={ROOT};subagent=x", "bad subagent hash"),
    ("LLM harness=cursor", "unknown harness cursor"),
    ("LLM effort=low", "harness required"),
    ("LLM model=a:b;harness=codex", "attributes out of order or duplicated"),
    ("LLM harness=codex;harness=codex", "model required"),
    (
        "LLM harness=codex;harness=codex;model=a:b",
        "attributes out of order or duplicated",
    ),
    ("LLM harness=claude-code", "model required"),
    ("LLM harness=claude-code:2.1;surface=cli", "model required"),
    ("LLM harness=codex:1;surface=cli", "model required"),
    ("LLM harness=antigravity", "model required"),
    ("LLM harness=codex;model=", "bad attribute 'model='"),
    ("LLM harness=codex;model=a,b", "bad attribute 'model=a,b'"),
]


@pytest.mark.parametrize(("value", "problem"), TRAILER_VALUE_CASES)
def test_reference_trailer_value_parity(value: str, problem: str | None) -> None:
    assert commit_grammar.check_value(value) == problem
    for key in (trailers.KEY_GEN, trailers.KEY_ASSIST):
        line = f"{key}: {value}"
        assert commit_grammar.check_message(line) == (
            [f"{line}: {problem}"] if problem else []
        )


def test_check_pair() -> None:
    assert commit_grammar.check_pair("harness=codex") is None
    assert commit_grammar.check_pair("surface=a:b/c") is None
    assert commit_grammar.check_pair("harness=") == "bad attribute 'harness='"
    assert commit_grammar.check_pair("=x") == "bad attribute '=x'"
    assert commit_grammar.check_pair("a=b=c") is not None
    for effort in EFFORTS:
        assert commit_grammar.check_pair(f"effort={effort}") is None
    assert (
        commit_grammar.check_pair("subagent=sha-256-64:ABCDEF0123456789")
        == "bad subagent hash"
    )
    assert (
        commit_grammar.check_pair("session=sha-256-64:" + "a" * 17)
        == "bad session hash"
    )
    assert commit_grammar.check_pair("harness=codex:1:2") is None


def test_check_message_orders_and_signoff() -> None:
    gen, assist = f"Generated-by: LLM {GOOD}", f"Assisted-by: LLM {GOOD}"
    assert commit_grammar.check_message(f"s\n\n{gen}\n{assist}\n") == []
    assert commit_grammar.check_message(f"s\n\n{assist}\n{gen}\n") == [
        f"{gen}: Generated-by must come before Assisted-by"
    ]
    assert (
        commit_grammar.check_message(f"s\n\n{gen}\n{gen}\n{assist}\n{assist}\n") == []
    )
    assert commit_grammar.check_message("s\n\nGenerated-by: nope\n") == [
        "Generated-by: nope: must start with literal LLM"
    ]
    # Sign-off and co-author trailers are not checked, for anyone.
    assert (
        commit_grammar.check_message(
            f"s\n\n{gen}\nSigned-off-by: H <h@x>\nCo-authored-by: C <c@x>\n"
        )
        == []
    )
    assert (
        commit_grammar.check_message("Generated-by:LLM harness=codex;model=m\n") == []
    )
    assert commit_grammar.check_message("  Generated-by: nope\n") == []
    assert commit_grammar.check_message("") == []


@settings(database=None)
@given(
    st.lists(
        st.sampled_from(
            ["effort", "harness", "model", "session", "subagent", "surface"]
        ),
        unique=True,
        min_size=1,
    )
)
def test_check_value_order_property(keys: list[str]) -> None:
    samples = CODEX_TRAILER_ATTRS
    ordered = [k for k in trailers.ATTR_ORDER if k in keys]
    value = "LLM " + ";".join(f"{k}={samples[k]}" for k in ordered)
    expected = None
    if "harness" not in keys:
        expected = "harness required"
    elif "model" not in keys:
        expected = "model required"
    assert commit_grammar.check_value(value) == expected
    shuffled = "LLM " + ";".join(f"{k}={samples[k]}" for k in reversed(ordered))
    if len(ordered) > 1 and {"harness", "model"} <= set(keys):
        assert (
            commit_grammar.check_value(shuffled)
            == "attributes out of order or duplicated"
        )


def test_check_pair_splits_at_the_first_equals_sign() -> None:
    assert commit_grammar.check_pair("model=a=b") is None
    assert commit_grammar.check_pair("model=a=b=c") is None
    assert commit_grammar.check_pair("harness=codex:1:2") is None
    assert commit_grammar.check_pair("harness=x:codex") == "unknown harness x:codex"


def test_check_value_splits_keys_at_the_first_equals_sign() -> None:
    assert commit_grammar.check_value("LLM harness=codex;model=a=b") is None
    assert commit_grammar.check_value("LLM harness=codex;model=a=b;surface=c=d") is None
    # Splitting at the last "=" would read these keys as surface=a and
    # surface=b and miss the duplicate.
    assert (
        commit_grammar.check_value("LLM harness=codex;model=m;surface=a=1;surface=b=1")
        == "attributes out of order or duplicated"
    )


def test_check_messages_are_exact() -> None:
    for line in ("Signed-off-by: A <a@b.c>", "Co-authored-by: A <a@b.c>"):
        assert commit_grammar.check_message(f"{line}\n") == []
    assert commit_grammar.check_value("LLM harness=codex;model=m") is None


def test_check_message_orders_attributes_alphabetically() -> None:
    ok = (
        "Generated-by: LLM effort=high;harness=codex;model=openai:m;session="
        + SESSION_HASH
    )
    assert commit_grammar.check_value(ok.removeprefix("Generated-by: ")) is None
    swapped = "LLM harness=codex;effort=high;model=m"
    assert (
        commit_grammar.check_value(swapped) == "attributes out of order or duplicated"
    )


def test_check_pair_validates_the_launch_fields() -> None:
    assert commit_grammar.check_pair(f"started_at={EARLY}") is None
    assert commit_grammar.check_pair(f"launch={LAUNCH}") is None
    assert commit_grammar.check_pair("launch=sha-256-64:XYZ") == "bad launch hash"
    bad = "2026-02-30T00:00:00Z"
    assert commit_grammar.check_pair(f"started_at={bad}") == f"bad started_at {bad}"
    assert commit_grammar.check_pair("started_at=2026-11-14t00:02:41z") is not None
    assert commit_grammar.check_pair("Started_at=x") == "bad attribute 'Started_at=x'"
    assert commit_grammar.check_pair("a_b=x") == "unknown attribute a_b"
