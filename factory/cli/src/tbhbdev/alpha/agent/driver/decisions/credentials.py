"""Reject credentials in text sent to agent runs."""

import re

from tbhbdev.alpha.agent.model import Invalid

_SECRET_PATTERNS = (
    ("1Password reference", re.compile(r"op://")),
    (
        "private key header",
        re.compile(
            r"-----BEGIN (?:(?:[A-Z0-9 ]+ )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
        ),
    ),
    ("token prefix", re.compile(r"ghp_|github_pat_|sk-ant-|AKIA|xoxb-")),
)


def check_secret(text: str, description: str) -> None:
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            message = f"{description} refused: {kind}"
            raise Invalid(message)
