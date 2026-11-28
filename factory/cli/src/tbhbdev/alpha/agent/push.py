"""Run the harness and record local build commits."""

from __future__ import annotations

import re
import subprocess  # nosec B404
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import host_tools, runfiles
from tbhbdev.alpha.agent.model import Profile

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import ExitStack

    from tbhbdev.alpha.agent.run_host.state import Prepared, Run

HISTORY_TIMEOUT = 30


class PublicationRefusedError(ValueError):
    """The run has new history that cannot be recorded as its head."""


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
    before, _ = local_refs(workspace)
    run.files.write("base", before[reference])
    if (start_ref := run.request.start_ref) is not None:
        run.files.write("log", f"start ref: {start_ref} at {before[reference]}\n")

    rc = harness(run, prepared, stack)
    if run.files.uncertain:
        return rc
    try:
        after, checkout = local_refs(workspace)
        if rc:
            tip = recovery_tip(reference, before, after)
        else:
            found = new_commits(run, workspace, before, after, checkout)
            tip = found[0][1] if found else None
        if tip is not None:
            run.files.write("head", tip)
    except PublicationRefusedError as exc:
        run.files.write("log", f"commit check failed: {exc}\n")
        return 4
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if rc:
            return rc
        run.files.write(
            "log",
            f"commit check failed: cannot check new commits ({type(exc).__name__})\n",
        )
        return 4
    return rc


def recovery_tip(ref: str, before: dict[str, str], after: dict[str, str]) -> str | None:
    if (tip := after.get(ref)) is None or tip in before.values():
        tip = after.get("HEAD") or after.get("detached HEAD")
    return None if tip in before.values() else tip


def local_refs(workspace: Path) -> tuple[dict[str, str], str]:
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
    return refs, current


def new_commits(
    run: Run,
    workspace: Path,
    before: dict[str, str],
    after: dict[str, str],
    checkout: str,
) -> list[tuple[str, str]]:
    """Find independent local branch or detached HEAD tips with new run history."""
    reference = f"refs/heads/agent/{run.files.name}"
    start = before[reference]
    # Preparation fetches into the clone before the harness can change its refs.
    bases = tuple(dict.fromkeys(before.values()))
    candidates = {
        name: tip
        for name, tip in after.items()
        if (name.startswith("refs/heads/") or name == "detached HEAD")
        and tip not in bases
    }
    if not candidates:
        return []
    with publication(
        run.files.root, workspace, next(iter(candidates.values()))
    ) as directory:
        rewrite_ref = reference if run.request.start_ref is not None else None
        found = eligible_tips(directory, candidates, bases, start, rewrite_ref)
        if found and checkout == f"ref: {reference}":
            if reference in found:
                return [(reference, found[reference])]
            message = "checked-out run branch has no new commits"
            raise PublicationRefusedError(message)
        if len(found) <= 1:
            return list(found.items())
        independent = set(
            history_git(
                directory, "merge-base", "--independent", *found.values()
            ).split()
        )
        return [(name, tip) for name, tip in found.items() if tip in independent]


def eligible_tips(
    directory: Path,
    candidates: dict[str, str],
    bases: tuple[str, ...],
    start: str,
    rewrite_ref: str | None = None,
) -> dict[str, str]:
    """Keep new run history, allowing rewrites on the selected run branch."""
    found: dict[str, str] = {}
    dropped: set[str] = set()
    for name, tip in sorted(candidates.items()):
        ancestor = None if name == rewrite_ref else start
        if outside_history(directory, tip, bases, ancestor):
            found[name] = tip
        elif ancestor and outside_history(directory, tip, bases):
            dropped.add(tip)
    if dropped:
        short = " ".join(tip[:12] for tip in sorted(dropped))
        message = f"{len(dropped)} new tip(s) outside run start ancestry: {short}"
        raise PublicationRefusedError(message)
    return found


def outside_history(
    directory: Path, tip: str, bases: tuple[str, ...], start: str | None = None
) -> str:
    """Return a new commit SHA, optionally restricted to descendants of the start."""
    revisions = "\n".join((tip, *(f"^{base}" for base in bases))) + "\n"
    # Excluding the start also excludes its ancestors.
    ancestry = (f"--ancestry-path={start}",) if start else ()
    arguments = ("rev-list", *ancestry, "--max-count=1", "--stdin", "--")
    return history_git(directory, *arguments, revisions=revisions).strip()


def remote_git(directory: Path, *args: str, token: str | None) -> str:
    """Use the same protected metadata for authenticated remote Git operations."""
    env = host_tools.git_token_env(token)
    return history_git(directory, *args, extra_env=env).strip()


def history_git(
    directory: Path,
    *args: str,
    revisions: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run a Git history query with the protected repository metadata."""
    env = dict.fromkeys(
        (
            "HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ),
        str(directory),
    )
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
        PATH=host_tools.search(),
    )
    env.update(extra_env or {})
    return subprocess.run(  # nosec B603
        [
            host_tools.resolve("git"),
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(directory),
            *args,
        ],
        cwd=directory,
        env=env,
        input=revisions,
        capture_output=True,
        text=True,
        check=True,
        timeout=HISTORY_TIMEOUT,
    ).stdout


def head(workspace: Path, branch: str) -> str:
    """Read refs as data; never load the sandbox's git configuration."""
    directory = workspace / ".git"
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


@contextmanager
def publication(root: Path, workspace: Path, commit: str) -> Generator[Path]:
    """Use the clone's object data through host-owned, temporary git metadata.

    The protected run state holds the metadata, outside every sandbox write
    root. Git never loads a config, hook, ref or helper from the run clone.
    """
    with tempfile.TemporaryDirectory(dir=root, prefix="push-") as temporary:
        directory = Path(temporary)
        (directory / "refs").mkdir()
        objects = directory / "objects" / "info"
        objects.mkdir(parents=True)
        (objects / "alternates").write_text(str(workspace / ".git" / "objects") + "\n")
        (directory / "config").write_text(
            "[core]\nrepositoryformatversion = 0\nbare = true\nfsmonitor = false\n"
            "[http]\nfollowRedirects = false\n"
            "[transfer]\nfsckObjects = true\n"
            "[user]\nname = Run launcher\nemail = run-launcher\n"
        )
        (directory / "HEAD").write_text(commit + "\n")
        yield directory
