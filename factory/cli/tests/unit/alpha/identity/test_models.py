# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity models."""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from unit.alpha.identity.identity_examples import (
    CHILD,
    CODEX_TRAILER_ATTRS,
    ROOT,
    make_receipt,
)

from tbhbdev.alpha.identity import models, trailers


def test_detect_innermost_wins() -> None:
    assert models.detect({}) is None
    assert models.detect({"CLAUDE_CODE_SESSION_ID": ""}) is None
    assert models.detect({"CLAUDE_CODE_SESSION_ID": "a"}) == ("claude", "a")
    both = {"CLAUDE_CODE_SESSION_ID": "a", "CODEX_SESSION_ID": "b"}
    assert models.detect(both) == ("codex", "b")
    all3 = {**both, "ANTIGRAVITY_CONVERSATION_ID": "c"}
    assert models.detect(all3) == ("antigravity", "c")
    assert models.detect({"ANTIGRAVITY_CONVERSATION_ID": "c"}) == ("antigravity", "c")


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
    assert models.clean_value(value) == expected


@settings(database=None)
@given(st.text())
def test_clean_value_is_grammar_safe(text: str) -> None:
    cleaned = models.clean_value(text)
    if cleaned is not None:
        assert cleaned
        assert cleaned == cleaned.strip().lower()
        assert not re.search(r"[\s;,]", cleaned)


def test_slugs() -> None:
    assert models.SLUGS == {
        "claude": "claude-code",
        "codex": "codex",
        "antigravity": "antigravity",
    }


def test_make_ident_omits_unknowns() -> None:
    assert models.make_ident("claude", ROOT, models.Seen()) == models.Ident(
        ROOT, "claude-code"
    )
    seen = models.Seen("2.1.316", "m", "High", "cli")
    assert models.make_ident("claude", ROOT, seen, CHILD) == models.Ident(
        ROOT, "claude-code:2.1.316", CHILD, "anthropic:m", "high", "cli"
    )
    assert models.make_ident("claude", ROOT, models.Seen(effort="huge")).effort is None
    assert models.make_ident("claude", ROOT, models.Seen(surface="")).surface is None
    assert (
        models.make_ident("claude", ROOT, models.Seen(version="a b")).harness
        == "claude-code"
    )
    assert models.make_ident("claude", ROOT, models.Seen(model=5)).model is None
    assert models.make_ident("claude", ROOT, models.Seen(), "").subagent is None


def test_make_ident_agy_effort_only_from_table() -> None:
    known = models.make_ident(
        "antigravity", ROOT, models.Seen(model="gemini-3.8-flash-low")
    )
    assert (known.model, known.effort) == ("gcp.gemini:gemini-3.8-flash", "low")
    explicit = models.make_ident(
        "antigravity", ROOT, models.Seen(model="gemini-3.8-flash-low", effort="high")
    )
    assert explicit.effort == "high"
    unknown = models.make_ident(
        "antigravity", ROOT, models.Seen(model="gemini-3.8-flash-tiered")
    )
    assert (unknown.model, unknown.effort) == ("gemini-3.8-flash-tiered", None)
    assert models.make_ident("antigravity", ROOT, models.Seen()).effort is None
    other = models.make_ident("codex", ROOT, models.Seen(model="gemini-3.8-flash-low"))
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
    ident = models.make_ident(
        "antigravity", ROOT, models.Seen(model=model, version="1.5.7", surface="cli")
    )
    assert trailers.format_value(ident.attrs()) == "LLM " + expected.format(s=ROOT)


def test_ident_attrs_and_keys() -> None:
    who = models.Ident(ROOT, "codex", CHILD, "openai:m", "low", "exec")
    assert who.attrs() == {**CODEX_TRAILER_ATTRS, "surface": "exec"}
    assert models.Ident(ROOT, "codex").attrs() == {"harness": "codex", "session": ROOT}
    assert who.agent_key() == (ROOT, CHILD)
    receipt = make_receipt("p", who)
    assert models.Ident.from_receipt(receipt) == who
    bare = models.Ident.from_receipt({"root": ROOT, "harness": "codex"})
    assert bare == models.Ident(ROOT, "codex")


def test_detect_prefers_the_innermost_harness() -> None:
    env = {
        "CLAUDE_CODE_SESSION_ID": "c",
        "CODEX_SESSION_ID": "x",
        "ANTIGRAVITY_CONVERSATION_ID": "a",
    }
    assert models.detect(env) == (models.ANTIGRAVITY, "a")
    del env["ANTIGRAVITY_CONVERSATION_ID"]
    assert models.detect(env) == (models.CODEX, "x")
    del env["CODEX_SESSION_ID"]
    assert models.detect(env) == (models.CLAUDE, "c")
    assert models.detect({"CLAUDE_CODE_SESSION_ID": ""}) is None
    assert models.detect({}) is None


def test_version_per_harness() -> None:
    assert models.version("claude", {"CLAUDE_CODE_EXECPATH": "/x/2.1.316"}) == "2.1.316"
    assert models.version("claude", {}) is None
    assert models.version("claude", {"CLAUDE_CODE_EXECPATH": ""}) is None
    assert models.version("codex", {"CODEX_VERSION": "0.166.0"}) == "0.166.0"
    assert models.version("codex", {"CODEX_VERSION": ""}) is None
    assert models.version("codex", {}) is None
    assert models.version("antigravity", {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}) == (
        "1.4.12"
    )
    assert (
        models.version("antigravity", {"ANTIGRAVITY_LS_VERSION": "1.4.12"}) == "1.4.12"
    )
    assert models.version("antigravity", {"ANTIGRAVITY_LS_VERSION": ""}) is None
    assert models.version("antigravity", {}) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sdk-cli", "sdk-cli"),
        ("CLI", "CLI"),
        ("a b", None),
        ("a=b", None),
        ("a;b", None),
        ("a,b", None),
        ("", None),
        (None, None),
        (3, None),
    ],
)
def test_surface_ok(value: object, expected: str | None) -> None:
    assert models.surface_ok(value) == expected


def test_surface_per_harness() -> None:
    assert models.surface("claude", {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) == "sdk-cli"
    assert models.surface("claude", {"CLAUDE_CODE_ENTRYPOINT": "a b"}) is None
    assert models.surface("claude", {}) is None
    assert (
        models.surface("antigravity", {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}) == "cli"
    )
    assert models.surface("antigravity", {"ANTIGRAVITY_LS_VERSION": "1.4.12"}) is None
    assert models.surface("antigravity", {}) is None


def test_unknown_agy_model_keys() -> None:
    for key in (
        "gemini-4-argon-cyber-permissive",
        "gemini-3.8-flash-tiered",
        "gemini-3.8-flash",
        "gemini-4-argon-xhigh",
        "gemini-4-carbon-xhigh",
        "",
    ):
        assert models.agy_model(key) == (key, None)


def test_provider_model() -> None:
    assert (
        models.provider_model("claude", "claude-haiku-5-5")
        == "anthropic:claude-haiku-5-5"
    )
    assert models.provider_model("codex", "gpt-5.6-sol") == "openai:gpt-5.6-sol"
    assert models.provider_model("codex", "o3-mini") == "openai:o3-mini"
    assert models.provider_model("codex", "codex-mini") == "openai:codex-mini"
    assert models.provider_model("codex", "mystery") == "mystery"
    assert models.provider_model("codex", "xgpt-5") == "xgpt-5"
    assert models.provider_model("antigravity", "gemini-3.8-flash-medium") == (
        "gcp.gemini:gemini-3.8-flash"
    )
    assert models.provider_model("antigravity", "gemini-3.8-flash-tiered") == (
        "gemini-3.8-flash-tiered"
    )
    assert models.provider_model("claude", "<synthetic>") is None
    assert models.provider_model("claude", "a b") is None
    assert models.provider_model("claude", "a;b") is None
    assert models.provider_model("claude", "a,b") is None
    assert models.provider_model("claude", "") is None
    assert models.provider_model("claude", None) is None
    assert models.provider_model("other", "m") == "m"


AGY_TABLE_ROWS = {
    "claude-opus-5-5-high": ("anthropic", "claude-opus-5-5", "high"),
    "claude-opus-5-5-low": ("anthropic", "claude-opus-5-5", "low"),
    "claude-opus-5-5-medium": ("anthropic", "claude-opus-5-5", "medium"),
    "claude-sonnet-5-5-high": ("anthropic", "claude-sonnet-5-5", "high"),
    "claude-sonnet-5-5-low": ("anthropic", "claude-sonnet-5-5", "low"),
    "claude-sonnet-5-5-medium": ("anthropic", "claude-sonnet-5-5", "medium"),
    "gemini-3.8-flash-high": ("gcp.gemini", "gemini-3.8-flash", "high"),
    "gemini-3.8-flash-low": ("gcp.gemini", "gemini-3.8-flash", "low"),
    "gemini-3.8-flash-medium": ("gcp.gemini", "gemini-3.8-flash", "medium"),
    "gemini-4-argon-high": ("gcp.gemini", "gemini-4-argon", "high"),
    "gemini-4-argon-low": ("gcp.gemini", "gemini-4-argon", "low"),
    "gemini-4-argon-medium": ("gcp.gemini", "gemini-4-argon", "medium"),
    "gemini-4-carbon-high": ("gcp.gemini", "gemini-4-carbon", "high"),
    "gemini-4-carbon-low": ("gcp.gemini", "gemini-4-carbon", "low"),
    "gemini-4-carbon-medium": ("gcp.gemini", "gemini-4-carbon", "medium"),
    "gemini-4-hydrogen-high": ("gcp.gemini", "gemini-4-hydrogen", "high"),
    "gemini-4-hydrogen-low": ("gcp.gemini", "gemini-4-hydrogen", "low"),
    "gemini-4-hydrogen-medium": ("gcp.gemini", "gemini-4-hydrogen", "medium"),
    "gemini-4-hydrogen-xhigh": ("gcp.gemini", "gemini-4-hydrogen", "xhigh"),
    "gemini-pro-orchestrator": ("gcp.gemini", "gemini-pro-orchestrator", None),
    "gemini-pro-orchestrator-argon": (
        "gcp.gemini",
        "gemini-pro-orchestrator-argon",
        None,
    ),
    "gpt-oss-120b-medium": ("openai", "gpt-oss-120b", "medium"),
}


def test_agy_models_table_rows_are_exact() -> None:
    """Every row, so swapped ids or efforts between keys fail."""
    assert models.AGY_MODELS == AGY_TABLE_ROWS
    for key, (provider, model, effort) in AGY_TABLE_ROWS.items():
        assert models.agy_model(key) == (f"{provider}:{model}", effort)
