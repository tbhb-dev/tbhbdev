# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity models."""

import pytest

from tbhbdev.alpha.identity import models


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
