# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Resolve the Claude root and child identities behind a commit."""

from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.ancestry import ancestry
from tbhbdev.alpha.identity.claude_commands import (
    AGENT_ENV,
    AttributionError,
    match_command_agent,
)
from tbhbdev.alpha.identity.claude_model import (
    hookless_model,
    main_model,
    transcript_model,
)
from tbhbdev.alpha.identity.models import surface, version
from tbhbdev.alpha.identity.sessions import load_record
from tbhbdev.alpha.identity.values import (
    CLAUDE,
    HASH_RE,
    Committer,
    Seen,
    make_ident,
    sha256_64,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tbhbdev.alpha.identity.payload import Json


def claude_committer(
    raw: str, env: Mapping[str, str], command_lines: list[str] | None
) -> Committer:
    """The Claude agent running git commit, found by process-ancestry matching."""
    root = sha256_64(raw)
    project_path = env.get("CLAUDE_PROJECT_DIR")
    project = Path(project_path) if project_path else None
    root_rec = load_record(root, project=project)
    ver = version(CLAUDE, env) or root_rec.get("version")
    surf = surface(CLAUDE, env)
    if env.get(AGENT_ENV) and HASH_RE.fullmatch(env[AGENT_ENV]):
        sub = env[AGENT_ENV]  # rewrite path (comparison only)
    else:
        sub, _ambiguous = match_command_agent(
            root,
            ancestry() if command_lines is None else command_lines,
            strict=True,
            project=project,
        )
    child_rec: Json = load_record(sub, root=root, project=project) if sub else {}
    if sub and child_rec.get("root") != root:
        message = (
            "The subagent's session record is missing or belongs to another root. "
            "Retry after the subagent's session record is restored."
        )
        raise AttributionError(message)
    model = main_model(root_rec) or hookless_model(raw, env, sub=sub)
    root_ident = make_ident(
        CLAUDE, root, Seen(ver, model, root_rec.get("effort"), surf)
    )
    if sub:
        # Re-read the transcript at commit time: it can trail the hook that recorded it.
        child = child_rec.get("model") or transcript_model(child_rec.get("transcript"))
        ident = make_ident(
            CLAUDE, root, Seen(ver, child, child_rec.get("effort"), surf), sub
        )
    else:
        effort = env.get("CLAUDE_EFFORT") or root_rec.get("effort")
        ident = make_ident(CLAUDE, root, Seen(ver, model, effort, surf))
    return Committer(CLAUDE, ident, root_ident)
