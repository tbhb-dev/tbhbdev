# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Normalize model, version, effort and surface values from known harnesses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal
lazy from collections.abc import Mapping

from tbhbdev.alpha.identity.records import Json, as_str

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
BAD_VALUE = re.compile(r"[\s;,]")
SURFACE_VALUE = re.compile(r"[^\s;,=]+")
CODEX_MODEL = re.compile(r"(gpt-|o\d|codex-)")

# Antigravity model keys -> (provider, canonical model id, effort or None).
# Source of every entry: `agy models` on 1.5.7 (key plus display name, such as
# "Gemini 3.8 Flash (Medium)"). Effort is the key suffix, confirmed by the
# display-name parenthetical in each case. Each base has its own effort set:
# gemini-4-hydrogen has xhigh, and the other bases stop at high. The two
# orchestrator keys carry no effort suffix, so their entries have no effort, and
# their model id is the key itself: the display name ("Orchestrator (Carbon)")
# names an underlying model, but nothing observed says which model answers. The
# gemini-* canonical IDs are inferred from display names and the old design,
# NOT verified against Google's API. Never regex-strip: unknown keys
# (gemini-3.8-flash-tiered, ...) pass through whole.
AGY_EFFORTS = ("low", "medium", "high")
AGY_MODELS: dict[str, tuple[str, str, str | None]] = {
    f"{base}-{effort}": (provider, base, effort)
    for base, provider, efforts in (
        ("gemini-4-argon", "gcp.gemini", AGY_EFFORTS),
        ("gemini-4-carbon", "gcp.gemini", AGY_EFFORTS),
        ("gemini-4-hydrogen", "gcp.gemini", (*AGY_EFFORTS, "xhigh")),
        ("gemini-3.8-flash", "gcp.gemini", AGY_EFFORTS),
        ("claude-opus-5-5", "anthropic", AGY_EFFORTS),
        ("claude-sonnet-5-5", "anthropic", AGY_EFFORTS),
    )
    for effort in efforts
}
AGY_MODELS["gpt-oss-120b-medium"] = ("openai", "gpt-oss-120b", "medium")
for _orchestrator in ("gemini-pro-orchestrator", "gemini-pro-orchestrator-argon"):
    AGY_MODELS[_orchestrator] = ("gcp.gemini", _orchestrator, None)


def version(
    key: Literal["claude", "codex", "antigravity"], env: Mapping[str, str]
) -> str | None:
    """The harness version from its environment, or None."""
    if key == "claude":
        path = env.get("CLAUDE_CODE_EXECPATH")
        return PurePath(path).name if path else None
    if key == "codex":
        return env.get("CODEX_VERSION") or None
    ls_version = env.get("ANTIGRAVITY_LS_VERSION")
    return ls_version.removeprefix("cli-") if ls_version else None


def surface_ok(value: object) -> str | None:
    """A raw vendor surface value (case preserved) if the grammar allows it."""
    if isinstance(value, str) and SURFACE_VALUE.fullmatch(value):
        return value
    return None


def surface(
    key: Literal["claude", "antigravity"], env: Mapping[str, str]
) -> str | None:
    """The surface a harness runs on (IDE, CLI, SDK and so on), from its env.

    Codex has none in its env; its surface is the rollout's ``source``.
    """
    if key == "claude":
        return surface_ok(env.get("CLAUDE_CODE_ENTRYPOINT"))
    ls_version = env.get("ANTIGRAVITY_LS_VERSION", "")
    return "cli" if ls_version.startswith("cli-") else None


def agy_model(raw: str) -> tuple[str, str | None]:
    """Explicit table lookup only: (model value, effort or None).

    Unknown keys pass through whole.
    """
    entry = AGY_MODELS.get(raw)
    if entry:
        return f"{entry[0]}:{entry[1]}", entry[2]
    return raw, None


def provider_model(key: str, model: str | None) -> str | None:
    """``provider:model`` for a raw model name, or None if unusable."""
    if not model or model.startswith("<") or BAD_VALUE.search(model):
        return None
    if key == "claude":
        return f"anthropic:{model}"
    if key == "codex" and CODEX_MODEL.match(model):
        return f"openai:{model}"
    if key == "antigravity":
        return agy_model(model)[0]
    return model


CLAUDE = "claude"


CODEX = "codex"


ANTIGRAVITY = "antigravity"


SLUGS = {CLAUDE: "claude-code", CODEX: "codex", ANTIGRAVITY: "antigravity"}


@dataclass(frozen=True)
class Ident:
    """One agent's final trailer values. ``root`` is the root session hash."""

    root: str
    harness: str
    subagent: str | None = None
    model: str | None = None
    effort: str | None = None
    surface: str | None = None
    launch: str | None = None
    started_at: str | None = None

    @classmethod
    def from_receipt(cls, receipt: Json) -> Ident:
        """Rebuild an identity from a stored edit receipt."""
        return cls(
            root=str(receipt["root"]),
            harness=str(receipt["harness"]),
            subagent=as_str(receipt.get("subagent")),
            model=as_str(receipt.get("model")),
            effort=as_str(receipt.get("effort")),
            surface=as_str(receipt.get("surface")),
        )

    def attrs(self) -> dict[str, str]:
        """Trailer attributes; unknown values are omitted."""
        found = {
            "effort": self.effort,
            "harness": self.harness,
            "launch": self.launch,
            "model": self.model,
            "session": self.root,
            "started_at": self.started_at,
            "subagent": self.subagent,
            "surface": self.surface,
        }
        return {k: v for k, v in found.items() if v}

    def agent_key(self) -> tuple[str, str | None]:
        """Which agent this is, independent of its model or effort."""
        return (self.root, self.subagent)


@dataclass(frozen=True)
class Seen:
    """Raw, unvalidated observations about one agent (any JSON-ish value)."""

    version: object = None
    model: object = None
    effort: object = None
    surface: object = None


@dataclass(frozen=True)
class Committer:
    """The agent running ``git commit``, plus its root agent."""

    key: str
    ident: Ident
    root_ident: Ident


def detect(env: Mapping[str, str]) -> tuple[str, str] | None:
    """Return (harness key, raw conversation ID), or None outside a harness.

    The innermost harness wins: agy, then codex, then claude. For agy this is
    the executing conversation (possibly a subagent); for codex, the root
    session.
    """
    for key, var in (
        (ANTIGRAVITY, "ANTIGRAVITY_CONVERSATION_ID"),
        (CODEX, "CODEX_SESSION_ID"),
        (CLAUDE, "CLAUDE_CODE_SESSION_ID"),
    ):
        if env.get(var):
            return key, env[var]
    return None


def clean_value(value: object) -> str | None:
    """Lowercase a trailer value; None when empty or not grammar-safe."""
    text = str(value).strip().lower() if value is not None else ""
    if not text or BAD_VALUE.search(text) or text == "<synthetic>":
        return None
    return text


def model_effort(
    key: str, raw_model: object, raw_effort: object
) -> tuple[str | None, str | None]:
    """Final (model, effort) trailer values from raw observations."""
    model, effort = as_str(raw_model), raw_effort
    if key == ANTIGRAVITY and model and not effort:
        effort = agy_model(model)[1]  # only from the verified table
    cleaned_effort = clean_value(effort)
    return provider_model(key, model), (
        cleaned_effort if cleaned_effort in EFFORTS else None
    )


def make_ident(
    key: str, session: str, seen: Seen, subagent: str | None = None
) -> Ident:
    """Final trailer values from raw-ish parts. Unknowns are omitted, never guessed."""
    model, effort = model_effort(key, seen.model, seen.effort)
    cleaned_version = clean_value(seen.version)
    return Ident(
        root=session,
        harness=SLUGS[key] + (f":{cleaned_version}" if cleaned_version else ""),
        subagent=subagent or None,
        model=model,
        effort=effort,
        surface=as_str(seen.surface),
    )
