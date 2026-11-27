# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Stamp and validate commit messages from the Git hooks."""

from __future__ import annotations

import sys
from pathlib import Path
lazy from collections.abc import Mapping

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import storage
from tbhbdev.alpha.identity.attribution import committer_identity
from tbhbdev.alpha.identity.codex.rollout import refresh_codex
from tbhbdev.alpha.identity.commit.claims import write_claim
from tbhbdev.alpha.identity.commit.grammar import check_message
from tbhbdev.alpha.identity.commit.inputs import enrich, staged_info, unstaged_blobs
from tbhbdev.alpha.identity.commit.message import add_trailers, amend_only, with_start
from tbhbdev.alpha.identity.commit.picking import usable_trailers
from tbhbdev.alpha.identity.commit.policy import (
    RUN_HOOK_ENV,
    RUN_MODEL_ENV,
    SESSION_ERROR,
    START_WARNING,
    harness_identity_problems,
    needs_git_identity,
    uses_agent_email,
)
from tbhbdev.alpha.identity.commit.replay import (
    amended_trailers,
    pick_head,
    pick_refusal,
    picked_commit,
    rebasing,
)
from tbhbdev.alpha.identity.commit.roles import (
    committing,
    decide,
    pick_roles,
    with_run_model,
)
from tbhbdev.alpha.identity.diagnostics import PROG
from tbhbdev.alpha.identity.models import Ident, detect
from tbhbdev.alpha.identity.receipts import load_edits
from tbhbdev.alpha.identity.records import AttributionError
from tbhbdev.alpha.identity.trailers import KEY_GEN, llm_trailers, trailer_start
from tbhbdev.alpha.repo import branch_created_at


def cmd_trailer(msgfile: str, source: str | None, env: Mapping[str, str]) -> int:
    """prepare-commit-msg: stamp ``Generated-by`` and ``Assisted-by``.

    An amend keeps every ``LLM`` trailer of the commit it replaces, even with a
    new ``-m``: post-commit consumed that commit's receipts, so they cannot be
    rebuilt. With no matching receipt, only an amended commit's trailers or a
    rebase's replayed message can suppress the committer fallback.

    A cherry-pick keeps the picked commit's ``LLM`` trailers, read from that
    commit, and never credits the picker as ``Generated-by``: the picker is
    ``Assisted-by`` when it has receipts on the staged paths, and nothing
    otherwise. Our own agent's commit with no ``Generated-by`` model is refused.

    With real git, ``CHERRY_PICK_HEAD`` exists while this hook runs in all three
    cases: a single pick, each commit of a multi-commit pick, and ``--continue``
    after a conflict. Git passes ``merge`` as the source whenever the message
    comes from ``MERGE_MSG``, which is a conflicted pick, ``--continue`` and
    ``--edit``, so a pick in progress is not a merge and the early return below
    leaves it alone. The ``-x`` line is the fallback for a missing
    ``CHERRY_PICK_HEAD``.

    Git also passes ``merge`` for every merge commit, ``git merge --no-commit``
    followed by a plain ``git commit`` included. An agent session stamps that
    commit like an ordinary one, so a run can commit a merge of the base branch.
    A human merge, a squash and a rebase replay stay unstamped.

    A rebase also sets ``CHERRY_PICK_HEAD`` for each commit it replays, so while
    one is in progress nothing is a pick and the commit is handled as it always was.
    """
    replaying = rebasing()
    head = pick_head(replaying=replaying)
    agent_merge = source == "merge" and not replaying and detect(env)
    if not head and source in {"merge", "squash"} and not agent_merge:
        return 0
    repo = storage.repo_info()
    if not repo:
        return 0
    path = Path(msgfile)
    kept = amended_trailers(repo.top)
    committer = committer_identity(env)
    text = path.read_text()
    if not committer:
        amend_only(path, text, kept)
        return 0
    committer = with_run_model(committer, env.get(RUN_MODEL_ENV))
    picked = None if replaying else picked_commit(head, text, repo.top)
    problem = picked and pick_refusal(picked, text, repo.top)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    refresh_codex(committer, repo, env)
    paths, blobs = staged_info(repo.top)
    staged = set(paths)
    edits = committing(
        load_edits(repo, committer.ident.root), staged, unstaged_blobs(repo.top, staged)
    )
    gen, assisted = decide(committer, [rec for _, rec in edits], staged, blobs)
    inherited = kept + (llm_trailers(text) if replaying else [])
    if picked:
        gen, assisted = pick_roles(committer, edited=bool(edits))
        kept = [*kept, *usable_trailers(picked[1])]
    elif not edits and any(key == KEY_GEN for key, _ in inherited):
        gen, assisted = list[Ident](), list[Ident]()
    # Enriching can make two identities of one agent equal, so deduplicate again.
    gen = list(dict.fromkeys(with_start(enrich(i, committer)) for i in gen))
    assisted = [with_start(i) for i in assisted]
    add_trailers(path, gen, assisted, kept, branch_start=branch_created_at(repo.top))
    write_claim(repo, committer.ident.root, [p for p, _ in edits])
    return 0


def ident_problems(text: str, env: Mapping[str, str]) -> list[str]:
    """Why a commit fails the agent identity's session or attribution rules.

    Every agent commit is authored and committed as ``AGENT_IDENT``, and
    ``text`` carries no ``Signed-off-by`` or ``Co-authored-by`` line, whoever it
    names; the operator signs off. ``git var`` gives the identities this commit
    records, including ``--author``, the author ``--amend`` keeps, and ``GIT_*``
    overrides. Outside a harness, the agent email requires a Generated-by
    trailer; human identities aren't checked, even when crediting agent edits.
    """
    if detect(env) is None:
        if needs_git_identity(text):
            for role in ("AUTHOR", "COMMITTER"):
                _, identity = git_repo.git("var", f"GIT_{role}_IDENT")
                if uses_agent_email(identity):
                    return [SESSION_ERROR]
        return []
    _, author = git_repo.git("var", "GIT_AUTHOR_IDENT")
    _, committer = git_repo.git("var", "GIT_COMMITTER_IDENT")
    return harness_identity_problems(text, author, committer)


def cmd_check(msgfile: str, env: Mapping[str, str]) -> int:
    """commit-msg: validate the LLM trailers and an agent commit's identity."""
    text = Path(msgfile).read_text()
    errs = check_message(text) + ident_problems(text, env)
    for err in errs:
        print(err if err == SESSION_ERROR else f"{PROG}: {err}", file=sys.stderr)
    if detect(env) and any(
        k == KEY_GEN and not trailer_start(v) for k, v in llm_trailers(text)
    ):
        print(f"{PROG}: warning: {START_WARNING}", file=sys.stderr)
    return 1 if errs else 0


def trailer_command(msgfile: str, source: str | None, env: Mapping[str, str]) -> int:
    if msgfile == "--dry-run":
        return 0
    required = env.get(RUN_HOOK_ENV) == "1"
    if required and detect(env) is None:
        raise AttributionError(SESSION_ERROR)
    status = cmd_trailer(msgfile, source, env)
    if (
        required
        and status == 0
        and not any(
            key == KEY_GEN for key, _ in llm_trailers(Path(msgfile).read_text())
        )
    ):
        message = "run commit requires a Generated-by trailer"
        raise AttributionError(message)
    return status
