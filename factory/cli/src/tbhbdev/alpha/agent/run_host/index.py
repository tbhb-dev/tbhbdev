"""Encode the index entry for a run and its outcome."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import request

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import RunRequest


@dataclass(frozen=True)
class IndexRun:
    request: RunRequest
    name: str
    host: str
    started: float
    worktree: str | None
    claude_profile: str | None


def index_entry(
    run: IndexRun, rc: int | None, *, ended: float | None
) -> dict[str, object]:
    fields = request.to_dict(run.request)
    return {
        key: fields[key]
        for key in (
            "v",
            "kind",
            "harness",
            "model",
            "account",
            "repo",
            "class",
            "variant",
            "reason",
            "role",
            "unsandboxed",
            "authorized_by",
        )
    } | {
        "run": run.name,
        "host": run.host,
        "worktree": run.worktree,
        "started": run.started,
        "ended": ended,
        "rc": rc,
        "cleaned": None,
        "kept": False,
        "claude_profile": run.claude_profile,
        "trace_id": None,
        "state": "running" if rc is None else "ended",
    }
