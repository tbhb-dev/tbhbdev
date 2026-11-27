# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Recognize harnesses and build normalized attribution identities."""

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.models import BAD_VALUE, EFFORTS, agy_model, provider_model
from tbhbdev.alpha.identity.payload import Json, as_str

if TYPE_CHECKING:
    from collections.abc import Mapping

PROG = "tbhbdev alpha identity"

RUN_HOOK_ENV = "TBHBDEV_REQUIRE_IDENTITY"

RUN_MODEL_ENV = "TBHBDEV_IDENTITY_MODEL"

CLAUDE = "claude"

CODEX = "codex"

ANTIGRAVITY = "antigravity"

# Internal harness key -> trailer slug.
SLUGS = {CLAUDE: "claude-code", CODEX: "codex", ANTIGRAVITY: "antigravity"}

ATTR_ORDER = [
    "effort",
    "harness",
    "launch",
    "model",
    "session",
    "started_at",
    "subagent",
    "surface",
    "variant",
]

HASH_RE = re.compile(r"sha-256-64:[0-9a-f]{16}")


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


def sha256_64(raw: object) -> str:
    """Hash a raw ID to ``sha-256-64:`` plus the first 16 hex of its SHA-256."""
    return "sha-256-64:" + hashlib.sha256(str(raw).encode()).hexdigest()[:16]


def hx(hashed: str) -> str:
    """The hex part of a ``sha-256-64:`` hash."""
    return hashed.split(":")[1]


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


def hash_ok(value: object) -> str | None:
    """A ``sha-256-64:`` hash, or None for anything else."""
    return value if isinstance(value, str) and HASH_RE.fullmatch(value) else None


def format_value(attrs: Mapping[str, str]) -> str:
    """Render ``LLM k=v;...`` in the grammar's attribute order."""
    return "LLM " + ";".join(f"{k}={attrs[k]}" for k in ATTR_ORDER if attrs.get(k))


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
