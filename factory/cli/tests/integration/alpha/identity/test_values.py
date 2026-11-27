# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity values tests."""

import hashlib
import re
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from integration.alpha.identity.identity_test_support import (
    CHILD,
    DATA,
    LAUNCH,
    ROOT,
    H,
    loaded,
    make_receipt,
)

from tbhbdev.alpha.identity import validation, values
from tbhbdev.alpha.identity.models import EFFORTS


def test_hash_vectors() -> None:
    vectors = loaded(DATA / "hash-vectors.json")["vectors"]
    assert isinstance(vectors, list)
    assert len(vectors) >= 3
    for vector in cast("list[dict[str, str]]", vectors):
        raw = bytes.fromhex(vector["input_hex"]).decode()
        assert raw == vector["input_utf8"]
        assert values.sha256_64(raw) == vector["expected"]


@given(st.text())
def test_sha256_64_shape(raw: str) -> None:
    hashed = values.sha256_64(raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    assert hashed == f"sha-256-64:{digest[:16]}"
    assert values.HASH_RE.fullmatch(hashed)
    assert values.hx(hashed) == digest[:16]


def test_sha256_64_hashes_the_string_form() -> None:
    assert values.sha256_64(12) == values.sha256_64("12")


def test_detect_innermost_wins() -> None:
    assert values.detect({}) is None
    assert values.detect({"CLAUDE_CODE_SESSION_ID": ""}) is None
    assert values.detect({"CLAUDE_CODE_SESSION_ID": "a"}) == ("claude", "a")
    both = {"CLAUDE_CODE_SESSION_ID": "a", "CODEX_SESSION_ID": "b"}
    assert values.detect(both) == ("codex", "b")
    all3 = {**both, "ANTIGRAVITY_CONVERSATION_ID": "c"}
    assert values.detect(all3) == ("antigravity", "c")
    assert values.detect({"ANTIGRAVITY_CONVERSATION_ID": "c"}) == ("antigravity", "c")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("High", "high"),
        ("  low  ", "low"),
        ("", None),
        ("   ", None),
        (None, None),
        ("a b", None),
        ("a;b", None),
        ("a,b", None),
        ("<synthetic>", None),
        (5, "5"),
    ],
)
def test_clean_value(value: object, expected: str | None) -> None:
    assert values.clean_value(value) == expected


@given(st.text())
def test_clean_value_is_grammar_safe(text: str) -> None:
    cleaned = values.clean_value(text)
    if cleaned is not None:
        assert cleaned
        assert cleaned == cleaned.strip().lower()
        assert not re.search(r"[\s;,]", cleaned)


def test_slugs() -> None:
    assert values.SLUGS == {
        "claude": "claude-code",
        "codex": "codex",
        "antigravity": "antigravity",
    }


def test_make_ident_omits_unknowns() -> None:
    assert values.make_ident("claude", ROOT, values.Seen()) == values.Ident(
        ROOT, "claude-code"
    )
    seen = values.Seen("2.1.316", "m", "High", "cli")
    assert values.make_ident("claude", ROOT, seen, CHILD) == values.Ident(
        ROOT, "claude-code:2.1.316", CHILD, "anthropic:m", "high", "cli"
    )
    assert values.make_ident("claude", ROOT, values.Seen(effort="huge")).effort is None
    assert values.make_ident("claude", ROOT, values.Seen(surface="")).surface is None
    assert (
        values.make_ident("claude", ROOT, values.Seen(version="a b")).harness
        == "claude-code"
    )
    assert values.make_ident("claude", ROOT, values.Seen(model=5)).model is None
    assert values.make_ident("claude", ROOT, values.Seen(), "").subagent is None


def test_make_ident_agy_effort_only_from_table() -> None:
    known = values.make_ident(
        "antigravity", ROOT, values.Seen(model="gemini-3.8-flash-low")
    )
    assert (known.model, known.effort) == ("gcp.gemini:gemini-3.8-flash", "low")
    explicit = values.make_ident(
        "antigravity", ROOT, values.Seen(model="gemini-3.8-flash-low", effort="high")
    )
    assert explicit.effort == "high"
    unknown = values.make_ident(
        "antigravity", ROOT, values.Seen(model="gemini-3.8-flash-tiered")
    )
    assert (unknown.model, unknown.effort) == ("gemini-3.8-flash-tiered", None)
    assert values.make_ident("antigravity", ROOT, values.Seen()).effort is None
    other = values.make_ident("codex", ROOT, values.Seen(model="gemini-3.8-flash-low"))
    assert other.effort is None


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            "gemini-4-hydrogen-xhigh",
            (
                "effort=xhigh;harness=antigravity:1.5.7;"
                "model=gcp.gemini:gemini-4-hydrogen;session={s};surface=cli"
            ),
        ),
        (
            "gemini-4-carbon-medium",
            (
                "effort=medium;harness=antigravity:1.5.7;"
                "model=gcp.gemini:gemini-4-carbon;session={s};surface=cli"
            ),
        ),
        (
            "gemini-pro-orchestrator-argon",
            (
                "harness=antigravity:1.5.7;"
                "model=gcp.gemini:gemini-pro-orchestrator-argon;session={s};surface=cli"
            ),
        ),
    ],
)
def test_agy_1_5_7_models_render_a_complete_trailer(model: str, expected: str) -> None:
    ident = values.make_ident(
        "antigravity", ROOT, values.Seen(model=model, version="1.5.7", surface="cli")
    )
    assert values.format_value(ident.attrs()) == "LLM " + expected.format(s=ROOT)


def test_ident_attrs_and_keys() -> None:
    who = values.Ident(ROOT, "codex", CHILD, "openai:m", "low", "exec")
    assert who.attrs() == {
        "effort": "low",
        "harness": "codex",
        "model": "openai:m",
        "session": ROOT,
        "subagent": CHILD,
        "surface": "exec",
    }
    assert values.Ident(ROOT, "codex").attrs() == {"harness": "codex", "session": ROOT}
    assert who.agent_key() == (ROOT, CHILD)
    receipt = make_receipt("p", who)
    assert values.Ident.from_receipt(receipt) == who
    bare = values.Ident.from_receipt({"root": ROOT, "harness": "codex"})
    assert bare == values.Ident(ROOT, "codex")


harnesses = st.sampled_from(sorted(values.SLUGS.values())).flatmap(
    lambda slug: st.one_of(
        st.just(slug),
        st.from_regex(r"[0-9]\.[0-9]{1,2}", fullmatch=True).map(
            lambda ver: f"{slug}:{ver}"
        ),
    )
)


hashes = st.from_regex(r"sha-256-64:[0-9a-f]{16}", fullmatch=True)


attr_sets = st.fixed_dictionaries(
    {
        "harness": harnesses,
        "model": st.from_regex(r"[a-z0-9][a-z0-9.:_-]{0,20}", fullmatch=True),
    },
    optional={
        "effort": st.sampled_from(sorted(EFFORTS)),
        "session": hashes,
        "subagent": hashes,
        "surface": st.from_regex(r"[A-Za-z0-9_-]{1,10}", fullmatch=True),
        "variant": st.sampled_from(["a", "b"]),
    },
)


@given(attr_sets)
def test_grammar_round_trip(attrs: dict[str, str]) -> None:
    value = values.format_value(attrs)
    assert value.startswith("LLM ")
    assert validation.check_value(value) is None
    pairs = [pair.split("=", 1) for pair in value.removeprefix("LLM ").split(";")]
    assert dict(pairs) == attrs
    keys = [key for key, _ in pairs]
    assert keys == [key for key in values.ATTR_ORDER if key in attrs]


def test_format_order_and_omission() -> None:
    attrs = {
        "surface": "cli",
        "subagent": H + "b" * 16,
        "model": "anthropic:m",
        "harness": "claude-code:2.1.316",
        "effort": "high",
        "session": ROOT,
    }
    assert values.format_value(attrs) == (
        "LLM effort=high;harness=claude-code:2.1.316;model=anthropic:m;"
        f"session={ROOT};subagent={H}{'b' * 16};surface=cli"
    )
    assert values.format_value({"harness": "antigravity"}) == "LLM harness=antigravity"
    assert values.format_value({"harness": "codex", "model": ""}) == "LLM harness=codex"
    assert sorted(values.ATTR_ORDER) == values.ATTR_ORDER
    assert {"launch", "started_at", "variant"} <= set(values.ATTR_ORDER)
    assert len(values.ATTR_ORDER) == 9


def test_detect_prefers_the_innermost_harness() -> None:
    env = {
        "CLAUDE_CODE_SESSION_ID": "c",
        "CODEX_SESSION_ID": "x",
        "ANTIGRAVITY_CONVERSATION_ID": "a",
    }
    assert values.detect(env) == (values.ANTIGRAVITY, "a")
    del env["ANTIGRAVITY_CONVERSATION_ID"]
    assert values.detect(env) == (values.CODEX, "x")
    del env["CODEX_SESSION_ID"]
    assert values.detect(env) == (values.CLAUDE, "c")
    assert values.detect({"CLAUDE_CODE_SESSION_ID": ""}) is None
    assert values.detect({}) is None


def test_hash_ok() -> None:
    assert values.hash_ok(LAUNCH) == LAUNCH
    assert values.hash_ok(LAUNCH + "0") is None
    assert values.hash_ok("x" + LAUNCH) is None
    assert values.hash_ok(None) is None
