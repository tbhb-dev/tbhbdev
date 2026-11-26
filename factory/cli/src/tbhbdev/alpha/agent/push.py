"""Run the harness and record local build commits."""

from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import ExitStack

    from tbhbdev.alpha.agent.run_host import Prepared, Run


def run(
    run: Run,
    prepared: Prepared,
    stack: ExitStack,
    harness: Callable[[Run, Prepared, ExitStack], int],
) -> int:
    if run.request.profile is not Profile.BUILD:
        return harness(run, prepared, stack)
    workspace = Path(prepared.paths.worktree)
    reference = f"refs/heads/agent/{run.files.name}"
    before = local_refs(workspace)
    base = before.get(reference)
    if base is not None:
        run.files.write("base", base)
    if (start_ref := run.request.start_ref) is not None:
        if base is None:
            message = f"missing fetched starting commit for {start_ref}"
            raise Invalid(message)
        run.files.write("log", f"start ref: {start_ref} at {base}\n")

    rc = harness(run, prepared, stack)
    if run.files.uncertain:
        return rc
    try:
        after = local_refs(workspace)
    except (OSError, ValueError) as exc:
        if rc:
            return rc
        run.files.write(
            "log",
            f"commit check failed: cannot read run refs ({type(exc).__name__})\n",
        )
        return 4
    tip = after.get(reference)
    if rc and (tip is None or tip in before.values()):
        tip = after.get("HEAD") or after.get("detached HEAD")
    if tip is not None and tip not in before.values():
        run.files.write("head", tip)
    return rc


def local_refs(workspace: Path) -> dict[str, str]:
    """Read loose and packed refs without loading the run's Git config."""
    directory = workspace / ".git"
    refs: dict[str, str] = {}
    with suppress(FileNotFoundError):
        packed = runfiles.read_owned(
            directory / "packed-refs", 1024 * 1024, "packed refs"
        )
        refs.update(
            (name, commit)
            for commit, name in re.findall(
                r"^([0-9a-f]{40}) (refs/\S+)$", packed, re.MULTILINE
            )
        )
    base = directory / "refs"
    for path in base.rglob("*"):
        if not path.is_dir() and path.suffix != ".lock":
            value = runfiles.read_owned(path, 1024, "run ref").strip()
            if re.fullmatch(r"[0-9a-f]{40}", value):
                refs[path.relative_to(directory).as_posix()] = value
    current = runfiles.read_owned(directory / "HEAD", 1024, "run HEAD").strip()
    if re.fullmatch(r"[0-9a-f]{40}", current):
        refs["detached HEAD"] = current
    elif tip := refs.get(current.removeprefix("ref: ")):
        refs["HEAD"] = tip
    return refs


def head(workspace: Path, branch: str) -> str | None:
    """Read refs as data; never load the sandbox's git configuration."""
    directory = workspace / ".git"
    if (
        runfiles.read_owned(directory / "HEAD", 1024, "run HEAD").strip()
        != f"ref: refs/heads/{branch}"
    ):
        return None
    reference = f"refs/heads/{branch}"
    try:
        value = runfiles.read_owned(directory / reference, 1024, "run ref").strip()
    except FileNotFoundError:
        packed = runfiles.read_owned(
            directory / "packed-refs", 1024 * 1024, "packed refs"
        )
        value = next(
            (
                line.split(" ")[0]
                for line in packed.splitlines()
                if line.endswith(f" {reference}")
            ),
            None,
        )
    if value is None:
        message = "missing run commit"
        raise ValueError(message)
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        message = "invalid run commit"
        raise ValueError(message)
    return value
