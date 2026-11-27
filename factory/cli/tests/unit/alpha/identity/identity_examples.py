# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Shared identity values used by attribution tests."""

from __future__ import annotations

import json
from dataclasses import replace

from tbhbdev.alpha.identity import models, records

H = "sha-256-64:"


ROOT = H + "a" * 16


CHILD = H + "c" * 16


OTHER = H + "d" * 16


type Json = dict[str, object]


def make_ident(
    sub: str | None = None,
    model: str | None = "anthropic:m",
    root: str = ROOT,
    effort: str | None = None,
    surface: str | None = None,
) -> models.Ident:
    return models.Ident(root, "claude-code", sub, model, effort, surface)


def make_receipt(
    path: str,
    who: models.Ident,
    blob: str | None = None,
    ts: float = 1,
) -> Json:
    return {
        "root": who.root,
        "subagent": who.subagent,
        "harness": who.harness,
        "model": who.model,
        "effort": who.effort,
        "surface": who.surface,
        "path": path,
        "blob": blob,
        "ts": ts,
        "consumed": False,
    }


SESSION_HASH = records.sha256_64("session-raw")


def patch_call(*paths: str) -> Json:
    body = "*** Begin Patch\n" + "".join(f"*** Add File: {p}\n+x\n" for p in paths)
    patch = json.dumps(body + "*** End Patch")
    code = f"const patch = {patch};\nawait tools.apply_patch(patch);"
    payload: Json = {"type": "custom_tool_call", "name": "exec", "input": code}
    return {"type": "response_item", "payload": payload}


GEN_SUB = ("Generated-by", f"LLM harness=claude-code;session={ROOT};subagent={CHILD}")


ASSIST_OTHER = ("Assisted-by", f"LLM harness=codex;session={OTHER}")


GOOD = f"harness=claude-code:2.1;model=anthropic:m;session={ROOT}"


HEAD = "type: Reference\ntitle: T\ndescription: D.\ntags:\n  - a-b\nstatus: draft\n"


AT1 = "2026-11-02T10:00:00Z"


AT2 = "2026-11-02T11:00:00Z"


type Entry = dict[str, str]


def entry(**fields: str) -> Entry:
    base = {
        "role": "generated",
        "harness": "claude-code:2.1.316",
        "model": "anthropic:claude-opus-5-5",
        "at": AT1,
    }
    return {**base, **fields}


def document(*lines: str, head: str = HEAD, tail: str = "", body: str = "# T\n") -> str:
    block = "".join(f"{line}\n" for line in lines)
    return f"---\n{head}{block}{tail}---\n{body}"


MAIN = models.Ident(
    SESSION_HASH, "claude-code", None, "anthropic:claude-opus-5-5", "high", "cli"
)


OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"


EARLIER = "2026-11-06T10:00:00.000Z"


EARLIER_TS = 1_793_959_200.0  # EARLIER in seconds


LAUNCH = H + "0a1b2c3d4e5f6071"


EARLY, LATE = "2026-11-14T00:02:41Z", "2026-11-14T00:09:00Z"


def line_of(start: str | None = None, extra: str = "", who: str = ROOT) -> str:
    stamp = f"started_at={start};" if start else ""
    base = f"LLM {extra}harness=claude-code;model=anthropic:m"
    return f"{base};session={who};{stamp}surface=cli"


VERSIONED_MAIN = replace(MAIN, harness="claude-code:2.1.316")

CODEX_TRAILER_ATTRS = {
    "effort": "low",
    "harness": "codex",
    "model": "openai:m",
    "session": ROOT,
    "subagent": CHILD,
    "surface": "cli",
}

CLAUDE_TRAILER_ATTRS = {
    "effort": "high",
    "harness": "claude-code",
    "model": "anthropic:claude-opus-5-5",
    "session": SESSION_HASH,
    "surface": "cli",
}


SRC_SESSION = records.sha256_64("source-session")


SRC_GEN = (
    "LLM harness=claude-code:2.1.316;model=anthropic:claude-opus-5-5;"
    f"session={SRC_SESSION};surface=cli"
)


SRC_GEN_NO_MODEL = f"LLM harness=claude-code:2.1.316;session={SRC_SESSION};surface=cli"


SHA_A = "a" * 40


SHA_B = "b" * 40
