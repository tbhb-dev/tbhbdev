# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read the file state needed by the operator script guard."""

import os
import posixpath
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.guard.scripts.decision import refusal, under
from tbhbdev.alpha.guard.scripts.paths import Place, normalize
from tbhbdev.alpha.guard.scripts.writes import targets
from tbhbdev.alpha.repo import hook_cwd

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tbhbdev.alpha.repo import Json


def main_repo_names(top: Path, dot_git: Path) -> list[str]:
    """The main repository's directory name, from a linked work tree's ``.git`` file."""
    try:
        gitdir = Path(top, dot_git.read_text().removeprefix("gitdir:").strip())
        common = (gitdir / "commondir").read_text().strip()
    except OSError, ValueError:
        return []
    path = Path(posixpath.normpath(posixpath.join(gitdir, common)))
    return [path.parent.name if path.name == ".git" else path.name]


def work_tree_names(path: str) -> list[str] | None:
    """The directory names that name the work tree around a path; None outside one."""
    for top in Path(path).parents:
        dot_git = top / ".git"
        if dot_git.is_dir():
            return [top.name]
        if dot_git.is_file():
            return [top.name, *main_repo_names(top, dot_git)]
    return None


def first_refusal(data: Json, env: Mapping[str, str]) -> str | None:
    """The deny message for the first refused write of a tool call, or None."""
    tmpdir = env.get("TMPDIR")
    place = Place(
        cwd=str(hook_cwd(data) or Path.cwd()),
        home=normalize(env["HOME"]),
        tmpdir=tmpdir and normalize(tmpdir),
    )
    libexec = f"{place.home}/.local/libexec/tbhb"
    for write in targets(data, place):
        in_libexec = under(write.path, libexec)
        exists = in_libexec and os.path.lexists(write.path)
        names = work_tree_names(write.path) if write.script and not in_libexec else None
        if reason := refusal(write, place, exists=exists, names=names):
            return reason
    return None
