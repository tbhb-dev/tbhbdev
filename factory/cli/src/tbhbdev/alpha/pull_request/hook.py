# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Check draft edits reported by a PostToolUse hook."""

import os
import sys
from pathlib import Path

from tbhbdev.alpha.identity import dispatcher as agent_identity
from tbhbdev.alpha.pull_request import draft_io as pr_draft_io
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.arguments import ANTIGRAVITY, CODEX
from tbhbdev.alpha.pull_request.draft import DRAFT
from tbhbdev.alpha.repo import hook_cwd, in_tbhb_dev, read_hook_stdin

AGY_EDITS = agent_identity.AGY_EDIT_TOOLS


# The exit code that makes a harness feed a PostToolUse hook's stderr to the agent.
HOOK_FEEDBACK = 2


def touched_paths(harness: str, data: agent_identity.Json) -> list[str]:
    """Files a harness's PostToolUse payload says the tool call wrote."""
    inputs = agent_identity.tool_input_of(data)
    path: object
    if harness == CODEX:
        return agent_identity.patch_paths(inputs.get("command"))
    if harness == ANTIGRAVITY:
        tool, args = agent_identity.agy_tool(data)
        path = agent_identity.agy_edit_path(args) if tool in AGY_EDITS else None
    else:
        path = inputs.get("file_path")
    return [path] if isinstance(path, str) and path else []


def hook(harness: str) -> int:
    """PostToolUse entry point: check the draft when the tool call wrote it.

    Exit 2 with the problems on stderr is what each harness feeds back to the
    agent. Any other call exits 0 without running a linter. The hook fails open:
    a crash or a missing tool prints a warning and exits 0, because a broken
    hook must never stop the agent's work.
    """
    try:
        data = read_hook_stdin()
        cwd = hook_cwd(data) or Path.cwd()
        if not in_tbhb_dev(cwd):
            return 0
        # The checks read the worktree's own schema, sections and configs.
        os.chdir(pr_tools.root(cwd))
        target = (pr_tools.root() / DRAFT).resolve()
        if not any((cwd / p).resolve() == target for p in touched_paths(harness, data)):
            return 0
        problems = pr_draft_io.load_checked(target).problems
    except (Exception, SystemExit) as exc:  # noqa: BLE001  # fail open on any crash
        print(f"pull request hook skipped: {exc}", file=sys.stderr)
        return 0
    if not problems:
        return 0
    print(f"{DRAFT} has problems. Fix them and save the file again.", file=sys.stderr)
    for line, why in problems:
        print(f"{DRAFT}:{line}: {why}", file=sys.stderr)
    return HOOK_FEEDBACK
