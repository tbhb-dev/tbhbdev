# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-agent-model`: make every subagent launch name a model.

A PreToolUse hook for the ``Agent`` tool. A subagent launched without ``model``
inherits the session's model, and a ``fork`` always does, so a session on a
costly model launches costly subagents by accident. The guard refuses a launch
whose ``tool_input.model`` is missing or empty by exiting 2 with the reason on
stderr, which Claude Code feeds back to the model. A ``fork`` has no ``model``
to check, so the guard reads the session's model from the transcript the hook
payload names: a fork passes when that model isn't costly (its id has neither
``opus`` nor ``fable``) and is refused when it is costly or can't be read. Any
other tool, and input it can't read, is allowed.
"""

import sys
from contextlib import suppress

from tbhbdev.alpha.identity.dispatcher import transcript_model
from tbhbdev.alpha.repo import Json, read_hook_stdin

TOOL = "Agent"
FORK = "fork"
CHOICE = "Pick sonnet for most work, and opus or fable only for planning and design."
REASON_MODEL = f"Agent launch refused: add `model` to the call. {CHOICE}"
COSTLY = ("opus", "fable")
TAIL = 400 * 1024


def reason_fork(model: str | None) -> str:
    """Why a fork is refused: it runs on ``model``, which is costly or unknown."""
    return (
        f"Agent launch refused: a fork runs on the session's model, "
        f"{model or 'unknown'}, which is costly or can't be read. Launch a "
        f"subagent with an explicit `model` instead. {CHOICE}"
    )


def verdict(data: Json, session_model: str | None) -> str | None:
    """The refusal reason for a hook payload, or None to allow it."""
    if data.get("tool_name") != TOOL:
        return None
    raw = data.get("tool_input")
    tool_input: Json = raw if isinstance(raw, dict) else {}
    if tool_input.get("subagent_type") == FORK:
        model = session_model
        costly = model is None or any(name in model.lower() for name in COSTLY)
        return reason_fork(model) if costly else None
    model = tool_input.get("model")
    if not isinstance(model, str) or not model.strip():
        return REASON_MODEL
    return None


def main() -> int:
    """Read the payload on stdin; exit 2 with the reason for a refused launch."""
    data = read_hook_stdin()
    raw = data.get("tool_input")
    model = None
    if (
        data.get("tool_name") == TOOL
        and isinstance(raw, dict)
        and raw.get("subagent_type") == FORK
    ):
        with suppress(ValueError):  # a path with a NUL byte
            model = transcript_model(data.get("transcript_path"), TAIL)
    reason = verdict(data, model)
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2
