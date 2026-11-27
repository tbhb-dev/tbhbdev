# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Resolve script paths against the hook working directory and environment."""

import posixpath
import re
from dataclasses import dataclass
from typing import NamedTuple

TMP_ROOT = "/tmp"  # noqa: S108  # nosec B108 -- the rule names /tmp
PRIVATE_TMP = re.compile(r"^/private(?=/tmp(?:/|$))")


class Write(NamedTuple):
    """A file a tool call writes: its normalized path, and whether it is a script."""

    path: str
    script: bool
    first_line: str | None = None


def normalize(raw: str, base: str = "/") -> str:
    """An absolute path with ``..`` resolved and ``/private/tmp`` read as ``/tmp``."""
    path = posixpath.normpath(posixpath.join(base, raw))
    # normpath keeps exactly two leading slashes, which name the same directory.
    return PRIVATE_TMP.sub("", "/" + path.lstrip("/"))


@dataclass(frozen=True)
class Place:
    """What paths resolve against: the cwd, the home directory and $TMPDIR."""

    cwd: str
    home: str
    tmpdir: str | None

    def expand(self, raw: str) -> str:
        """A word with a leading ``~``, ``$HOME`` or ``$TMPDIR`` expanded.

        An unset or empty ``$TMPDIR`` stays literal.
        """
        bases = {
            "~": self.home,
            "$HOME": self.home,
            "${HOME}": self.home,
            "$TMPDIR": self.tmpdir,
            "${TMPDIR}": self.tmpdir,
        }
        for prefix, base in bases.items():
            if base and (raw == prefix or raw.startswith(f"{prefix}/")):
                return base + raw[len(prefix) :]
        return raw

    def resolve(self, raw: str) -> str:
        """The normalized absolute path of a word, with variables expanded."""
        return normalize(self.expand(raw), self.cwd)

    def write(
        self, raw: str, *, content: bool = False, first_line: str | None = None
    ) -> Write:
        """A write to a word; a script when its content or its name says so."""
        path = self.resolve(raw)
        line = (
            first_line.partition("\n")[0]
            if path.endswith(".py") and first_line is not None
            else None
        )
        return Write(path, content or path.endswith((".sh", ".py")), line)
