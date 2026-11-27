# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Attribute pending commits from staged edits and session identities."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.attribution import (
    committer_identity,
    decide,
    enrich,
    with_run_model,
)
from tbhbdev.alpha.identity.claims import (
    committing,
    staged_info,
    unstaged_blobs,
    write_claim,
)
from tbhbdev.alpha.identity.codex_scan import refresh_codex
from tbhbdev.alpha.identity.commit_trailers import add_trailers, amend_only, with_start
from tbhbdev.alpha.identity.edits import load_edits
from tbhbdev.alpha.identity.replay import (
    amended_trailers,
    pick_head,
    pick_refusal,
    pick_roles,
    picked_commit,
    rebasing,
    usable_trailers,
)
from tbhbdev.alpha.identity.storage import repo_info
from tbhbdev.alpha.identity.trailers import KEY_GEN
from tbhbdev.alpha.identity.validation import llm_trailers
from tbhbdev.alpha.identity.values import RUN_MODEL_ENV, Ident, detect
from tbhbdev.alpha.repo import branch_created_at

if TYPE_CHECKING:
    from collections.abc import Mapping


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
    repo = repo_info()
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
