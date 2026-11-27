# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Define Git identities and the session rules for agent commits."""

from __future__ import annotations

import re

from tbhbdev.alpha.identity.trailers import KEY_GEN

RUN_HOOK_ENV = "TBHBDEV_REQUIRE_IDENTITY"


RUN_MODEL_ENV = "TBHBDEV_IDENTITY_MODEL"


IDENT_EMAIL = re.compile(r"<([^>]*)>")


def ident_email(ident: str) -> str:
    """The address in a ``Name <email> date`` identity; empty when there is none."""
    found = IDENT_EMAIL.search(ident)
    return found[1] if found else ""


AGENT_IDENT = "tbhb.dev Agent <agent@tonyburns.net>"


AGENT_EMAIL = AGENT_IDENT.partition("<")[2].removesuffix(">")


SESSION_ERROR = (
    "this commit claims the agent identity but no agent session is attached. "
    "Agents: commit from the harness's own host, where the session's hooks run. "
    "Humans: commit as yourself; the agent identity is reserved for agent sessions."
)


IDENT_DATE = re.compile(r" [0-9]+ [-+][0-9]{4}$")


SIGNOFF = re.compile(
    r"^(?:Signed-off-by|Co-authored-by)[ \t]*:", re.IGNORECASE | re.MULTILINE
)


SIGNOFF_ERROR = (
    "agent commits carry no Signed-off-by or Co-authored-by; the operator signs off"
)


START_WARNING = "a Generated-by line lacks started_at"


def needs_git_identity(text: str) -> bool:
    """An outside-harness commit needs the Git identity if it has no attribution."""
    return not any(line.startswith(f"{KEY_GEN}:") for line in text.splitlines())


def uses_agent_email(identity: str) -> bool:
    """Whether a Git author or committer uses the reserved address."""
    return IDENT_DATE.sub("", identity).endswith(f"<{AGENT_EMAIL}>")


def harness_identity_problems(text: str, author: str, committer: str) -> list[str]:
    """Check the identities and trailers recorded by a commit made in a harness."""
    errors = []
    for role, identity in (("author", author), ("committer", committer)):
        who = IDENT_DATE.sub("", identity)
        if who != AGENT_IDENT:
            errors.append(f"{role} is {who!r}; an agent commit must use {AGENT_IDENT}")
    if SIGNOFF.search(text):
        errors.append(SIGNOFF_ERROR)
    return errors
