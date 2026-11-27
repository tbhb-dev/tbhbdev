# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Decide whether a proposed script write belongs at its destination."""

import datetime as dt
import posixpath
import re
from typing import TYPE_CHECKING

from tbhbdev.alpha.guard.scripts.paths import TMP_ROOT

if TYPE_CHECKING:
    from tbhbdev.alpha.guard.scripts.paths import Place, Write


PLACE = (
    "Write scripts for the operator to "
    "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh or .py. A revision is a new "
    "file with a .vN suffix, because a handed-over script is never edited. "
    "Run logs go in ~/.local/state/tbhb/<same name>/."
)


NAME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+-[a-z0-9-]+(\.v[0-9]+)?\.(sh|py)"
)


PYTHON_SHEBANG = "#!/usr/bin/env -S python3 -I"


DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})-.*")


SCRATCHPAD = re.compile(r"/tmp/claude-[0-9]+/[^/]+/[^/]+/scratchpad/.+")  # noqa: S108 # nosec B108 -- the rule names /tmp


def valid_name(name: str) -> bool:
    """Whether a dated operator script name ends in ``.sh`` or ``.py``."""
    if not NAME.fullmatch(name) or not (date := DATE.fullmatch(name)):
        return False
    try:
        dt.date(int(date[1]), int(date[2]), int(date[3]))
    except ValueError:
        return False
    return True


def under(path: str, base: str) -> bool:
    """Whether ``path`` is ``base`` or below it."""
    return path == base or path.startswith(base.rstrip("/") + "/")


def libexec_refusal(
    path: str, libexec: str, first_line: str | None, *, exists: bool
) -> str | None:
    """The deny message for a write under the libexec directory, or None."""
    if exists:
        return (
            f"{path} already exists, and a handed-over script is never edited. "
            "Write the revision to a new file with a .vN suffix, such as "
            "YYYY-MM-DD-<owner>-<slug>.v2.sh or .v2.py."
        )
    if posixpath.dirname(path) != libexec or not valid_name(posixpath.basename(path)):
        return (
            f"{path} isn't a valid name for a script. Name it "
            "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh or .py, with a real "
            "date and no subdirectory. A revision is a new file with a .vN suffix."
        )
    if path.endswith(".py") and first_line is not None and first_line != PYTHON_SHEBANG:
        return f"{path} must start with {PYTHON_SHEBANG}."
    return None


def refusal(
    write: Write, place: Place, *, exists: bool, names: list[str] | None
) -> str | None:
    """The deny message for a write, or None when it is allowed."""
    path = write.path
    libexec = f"{place.home}/.local/libexec/tbhb"
    if under(path, libexec):
        return libexec_refusal(path, libexec, write.first_line, exists=exists)
    if not write.script:
        return None
    if names is not None:
        drafts = any(name.startswith("drafts") for name in names)
        return (
            f"A drafts repository doesn't hold scripts: {path}. {PLACE}"
            if drafts
            else None
        )
    run_scoped = (
        place.tmpdir is not None
        and place.tmpdir.startswith(TMP_ROOT + "/")
        and under(path, place.tmpdir)
    )
    if SCRATCHPAD.fullmatch(path) or run_scoped:
        return None
    if under(path, place.home) or under(path, TMP_ROOT):
        return f"Scripts for the operator don't go in {path}. {PLACE}"
    return None
