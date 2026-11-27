"""Validate the target and message of a steering request."""

from __future__ import annotations

from tbhbdev.alpha.agent.driver.decisions.prompt import check_secret
from tbhbdev.alpha.agent.model import Harness, Invalid

CLAUDE_UNAVAILABLE = "Claude delivery is not built"


def check_send(name: str, harness: Harness, text: str) -> None:
    if harness is Harness.CLAUDE:
        raise Invalid(CLAUDE_UNAVAILABLE)
    if harness is not Harness.CODEX:
        message = f"{name}: send requires a Codex run"
        raise Invalid(message)
    check_secret(text, "steering message")
