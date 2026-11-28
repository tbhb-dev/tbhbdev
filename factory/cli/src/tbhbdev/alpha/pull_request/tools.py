# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Resolve tools and run commands in the worktree."""

import os
import shutil
import subprocess  # nosec B404
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from tbhbdev.alpha import tool_pins
from tbhbdev.alpha.repo import git as repo_git

if TYPE_CHECKING:
    from collections.abc import Collection, Generator, Mapping, Sequence


SCC = "scc"
BAZEL = "bazelisk"
BAZEL_ENV = ".bazel_env"
GH = "gh"
GIT = "git"
COG = "cog"
GITLEAKS = "gitleaks"
MISE = "mise"


_TOOL_PATHS: ContextVar[Mapping[tuple[str, bool], str]] = ContextVar(
    "_TOOL_PATHS", default=MappingProxyType({})
)


def git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    """Run Git with the executable resolved during command startup, if any."""
    return repo_git(*args, cwd=cwd, executable=git_executable())


def root(cwd: Path | None = None) -> Path:
    """The top of the worktree containing ``cwd`` (default: the current one).

    Outside a worktree, the directory itself.
    """
    code, out = git("rev-parse", "--show-toplevel", cwd=cwd)
    return Path(out) if code == 0 else (cwd or Path.cwd())


def find_tool(name: str, *, unwrapped: bool = False) -> str:
    """Find a tool in the worktree's Bazel shims, on PATH, or through mise.

    ``unwrapped`` selects the raw shim. The gitleaks text scan writes its own
    config with ``[extend]``, which the rules wrapper refuses.
    """
    found = _TOOL_PATHS.get().get((name, unwrapped)) or _locate_tool(
        name, unwrapped=unwrapped
    )
    if found:
        return found
    message = (
        f"{name} not found in .bazel_env, on PATH or through `mise which`; "
        "run `bazelisk run //:bazel_env` or `mise install`"
    )
    raise SystemExit(message)


def _locate_tool(name: str, *, unwrapped: bool) -> str | None:
    """Return the selected executable after checking its version or pin, if any."""
    shim_name = f"{name}-raw" if unwrapped else name
    if shim := shutil.which(str(root() / BAZEL_ENV / "bin" / shim_name)):
        return tool_pins.require_reported_version(name, shim)
    path = None
    if unwrapped:
        entries = os.environ.get("PATH", "").split(os.pathsep)
        path = os.pathsep.join(e for e in entries if BAZEL_ENV not in e)
    found = shutil.which(name, path=path)
    if found:
        return tool_pins.require_version(name, found)
    mise = shutil.which(MISE, path=path)
    if mise:
        done = run([mise, "which", name])
        if done.returncode == 0 and done.stdout.strip():
            return tool_pins.require_version(name, done.stdout.strip())
    return None


def find_tools(
    names: Sequence[str], *, unwrapped: Collection[str] = ()
) -> dict[str, str]:
    """Resolve each tool or report all missing tools together."""
    paths: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        if found := _locate_tool(name, unwrapped=name in unwrapped):
            paths[name] = found
        else:
            missing.append(name)
    if missing:
        message = (
            f"missing tools: {', '.join(missing)}; "
            "run `bazelisk run //:bazel_env` or `mise install`"
        )
        raise SystemExit(message)
    return paths


def run(
    argv: Sequence[str], *, cwd: Path | None = None, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured output.

    Stdin is a pipe, never a terminal, so gh cannot prompt, and a prompt could
    push a branch.
    """
    # Argv comes from fixed names and validated draft fields.
    return subprocess.run(  # nosec B603
        list(argv),
        capture_output=True,
        text=True,
        check=False,  # pragma: no mutate -- the default, spelled for the linter
        cwd=cwd,
        input=stdin,
    )


def run_bytes(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Run a command and keep its output as bytes, so line endings stay as written."""
    # Argv comes from fixed names and a validated pull request number.
    return subprocess.run(  # nosec B603
        list(argv),
        capture_output=True,
        check=False,  # pragma: no mutate -- the default, spelled for the linter
        # An inherited stdin behaves like the empty pipe for these gh reads.
        input=b"",  # pragma: no mutate
    )


def git_executable() -> str:
    """Use the Git binary resolved for this command, or the default."""
    return _TOOL_PATHS.get().get((GIT, False), GIT)


@contextmanager
def resolved_tools(
    names: Sequence[str], *, unwrapped: Collection[str]
) -> Generator[None]:
    """Resolve and cache the tools for the duration of one command."""
    paths = find_tools(names, unwrapped=unwrapped)
    token = _TOOL_PATHS.set({(name, name in unwrapped): p for name, p in paths.items()})
    try:
        yield
    finally:
        _TOOL_PATHS.reset(token)
