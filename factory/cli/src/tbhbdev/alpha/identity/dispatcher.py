# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Dispatch the identity CLI commands and report hook failures."""

from __future__ import annotations

import sys
lazy import traceback
lazy from collections.abc import Mapping

lazy from tbhbdev.alpha.identity.antigravity.session import record_agy, save_agy_session
lazy from tbhbdev.alpha.identity.claude.hooks import handle_claude
lazy from tbhbdev.alpha.identity.codex.hooks import handle_codex
lazy from tbhbdev.alpha.identity.commit import claims
lazy from tbhbdev.alpha.identity.commit import hooks as commit_hooks
lazy from tbhbdev.alpha.identity.commit.hooks import trailer_command
lazy from tbhbdev.alpha.identity.diagnostics import PROG, hook_failure
lazy from tbhbdev.alpha.identity.documents import hooks as document_hooks
lazy from tbhbdev.alpha.identity.models import CLAUDE, CODEX, SLUGS, detect
lazy from tbhbdev.alpha.identity.payload import parse_stdin
lazy from tbhbdev.alpha.identity.records import AttributionError
lazy from tbhbdev.alpha.identity.storage import state_dir
lazy from tbhbdev.alpha.repo import hook_cwd, in_tbhb_dev, read_hook_stdin


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


USAGE = (
    "usage: tbhbdev alpha identity record <harness> | trailer <msgfile> [source]"
    " | trailer --dry-run"
    " | consume | check <msgfile> | stamp-docs"
)


def main(argv: list[str], env: Mapping[str, str]) -> int:
    """Dispatch a command line (``argv`` without the program name)."""
    match argv:
        case ["record", harness, *_]:
            return cmd_record(harness, env)
        case ["trailer", msgfile, *rest]:
            return trailer_command(msgfile, rest[0] if rest else None, env)
        case ["consume", *_]:
            return claims.cmd_consume()
        case ["stamp-docs", *_]:
            return document_hooks.cmd_stamp_docs(env)
        case ["check", msgfile, *_]:
            return commit_hooks.cmd_check(msgfile, env)
        case _:
            print(USAGE, file=sys.stderr)
            return 2


def run(argv: list[str], env: Mapping[str, str]) -> int:
    """``main``, reporting any crash as one line on stderr.

    The line names the exception type and where it was raised but not its
    message, which could quote a raw ID from hook input. A crashed ``record``,
    a harness hook, exits 0 so it never blocks the agent; the commit-time
    commands exit 1, so a commit without its identity checked doesn't land.
    """
    try:
        return main(argv, env)
    except AttributionError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- every failure becomes one clear line
        where = traceback.extract_tb(exc.__traceback__)[-1]
        message, code = hook_failure(
            argv[0], type(exc).__name__, where.name, where.lineno
        )
        print(message, file=sys.stderr)
        return code
