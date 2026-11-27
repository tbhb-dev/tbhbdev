# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Dispatch identity recording hooks for supported harnesses."""

from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.antigravity import record_agy, save_agy_session
from tbhbdev.alpha.identity.claude import handle_claude
from tbhbdev.alpha.identity.codex import handle_codex
from tbhbdev.alpha.identity.payload import parse_stdin
from tbhbdev.alpha.identity.storage import state_dir
from tbhbdev.alpha.identity.values import CLAUDE, CODEX, SLUGS, detect
from tbhbdev.alpha.repo import hook_cwd, in_tbhb_dev, read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Mapping


def cmd_record(harness: str, env: Mapping[str, str]) -> int:
    """Harness hook entry point: persist what the event reveals."""
    if harness not in SLUGS:
        return 0
    data = read_hook_stdin()
    cwd = hook_cwd(data)
    # A Claude session can later commit in a tbhb-dev clone even when rooted
    # elsewhere. In another owner's repository keep only its main user record:
    # no repository state, commands, edit receipts or subagent records.
    user_only = bool(state_dir(cwd) and not in_tbhb_dev(cwd))
    if user_only and harness != CLAUDE:
        return 0
    if harness == CODEX:
        handle_codex(data, env)
        return 0
    sid, model, effort = parse_stdin(data)
    if harness == CLAUDE:
        if sid:
            handle_claude({**data, "session_id": sid}, env, user_only=user_only)
        return 0
    sid = sid or (detect(env) or (None, None))[1]
    if not sid:
        return 0
    rec = save_agy_session(harness, sid, data, (model, effort), env)
    if rec is not None:
        record_agy(sid, data, rec, env)
    return 0
