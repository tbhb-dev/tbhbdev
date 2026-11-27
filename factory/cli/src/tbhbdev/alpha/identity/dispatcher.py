# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Dispatch harness and Git hooks for agent identity.

Harness hooks record observed sessions and edits. Git's prepare-commit-msg
hook stamps the attribution, commit-msg validates it, and post-commit
consumes the receipts. The pre-commit hook also stamps Markdown frontmatter.
"""

import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity.claims import cmd_consume
from tbhbdev.alpha.identity.claude_commands import AttributionError
from tbhbdev.alpha.identity.commit import cmd_trailer
from tbhbdev.alpha.identity.documents import cmd_stamp_docs
from tbhbdev.alpha.identity.record import cmd_record
from tbhbdev.alpha.identity.trailers import KEY_GEN
from tbhbdev.alpha.identity.validation import SESSION_ERROR, cmd_check, llm_trailers
from tbhbdev.alpha.identity.values import PROG, RUN_HOOK_ENV, detect

if TYPE_CHECKING:
    from collections.abc import Mapping

# prek and pre-commit pass the commit source here, not as an argument.
SOURCE_ENV = "PRE_COMMIT_COMMIT_MSG_SOURCE"


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
            return trailer_command(
                msgfile, rest[0] if rest else env.get(SOURCE_ENV), env
            )
        case ["consume", *_]:
            return cmd_consume()
        case ["stamp-docs", *_]:
            return cmd_stamp_docs(env)
        case ["check", msgfile, *_]:
            return cmd_check(msgfile, env)
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
        print(
            f"{PROG} {argv[0]}: failed: {type(exc).__name__} in "
            f"{where.name} (line {where.lineno})",
            file=sys.stderr,
        )
        return 0 if argv[0] == "record" else 1
