"""Temporary repositories and literal requests for host-side agent tests."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

lazy import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

import time

from agent_example import settings
from test_alpha_agent_request import BUILD

from tbhbdev.alpha.agent import build_info, model, runfiles, worktree_git
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.run_host import state


class HostGitOptions(TypedDict, closed=True):
    env: dict[str, str]
    text: Literal[True]
    stderr: int
    timeout: int
    cwd: str | Path


def isolate_host_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = subprocess.check_output

    def checked(command: list[str], **kwargs: Unpack[HostGitOptions]) -> str:
        if kwargs.get("cwd") == "/":
            directory = Path(command[command.index("--git-dir") + 1])
            assert directory.resolve().is_relative_to(tmp_path.resolve())
            kwargs["cwd"] = directory.parent if directory.name == ".git" else directory
        return output(command, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", checked)


def git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    child = dict(os.environ if env is None else env)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        child.pop(name, None)
    child["HOME"] = str(path)
    for name in ("CONFIG", "CACHE", "DATA", "STATE"):
        child[f"XDG_{name}_HOME"] = str(path / name.lower())
    return subprocess.check_output(
        ["git", "-C", str(path), *args],
        stderr=subprocess.DEVNULL,
        env=child,
        cwd=path,
        text=True,
        timeout=10,
    ).strip()


def commit(path: Path, message: str, *, env: dict[str, str] | None = None) -> str:
    git(path, "add", ".", env=env)
    git(
        path,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.com",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-qm",
        message,
        env=env,
    )
    return git(path, "rev-parse", "HEAD", env=env)


def repository(root: Path) -> tuple[Path, Path]:
    origin = root / "origin"
    seed = root / "seed"
    clone = root / "clone"
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "--bare", "--initial-branch=main", str(origin))
    git(root, "init", "--initial-branch=main", str(seed))
    (seed / "tracked").write_text("initial\n")
    commit(seed, "initial")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main")
    git(root, "clone", str(origin), str(clone))
    return seed, clone


def run_request(**changes: object) -> model.RunRequest:
    return request.parse(
        BUILD
        | {
            "repo": "tbhb-dev/example-repo",
            "max_concurrent_runs": 3,
            "unsandboxed": True,
        }
        | changes,
    )


def local_mirror_fetches(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    original = worktree_git.git

    def git(path: Path, *args: str, token: str | None = None) -> str:
        if path.name == "example-repo.git" and args[0] in ("fetch", "ls-remote"):
            args = tuple(str(origin) if value == "origin" else value for value in args)
        return original(path, *args, token=token)

    monkeypatch.setattr(worktree_git, "git", git)


def host_settings() -> model.Settings:
    original = settings()
    references = model.Credentials(
        "agent",
        "example/gh/value",
        "example/docker/value",
        "example/app/client",
        "example/app/pem",
        "example/signing/key",
    )
    return dataclasses.replace(
        original,
        credentials=(references,),
        kill_grace_seconds=0,
        claude=model.Claude(
            "primary", (model.ClaudeProfile("primary", "example/primary/token"),)
        ),
    )


def constant[Result](value: Result) -> Callable[..., Result]:
    def fixed(*_args: object, **_kwargs: object) -> Result:
        return value

    return fixed


def preparation_failure(
    error: type[Exception], *_args: object, **_kwargs: object
) -> None:
    if error is subprocess.CalledProcessError:
        raise subprocess.CalledProcessError(
            1, "sensitive-command", stderr="sensitive-stderr"
        )
    message = "sensitive-exception"
    raise error(message)


def build_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, present: bool
) -> dict[str, str]:
    checkout = tmp_path / "tools"
    checkout.mkdir()
    source = checkout / "factory/cli/src/tbhbdev/alpha/agent/build_info.py"
    monkeypatch.setattr(build_info, "SOURCE", source)
    if present:
        (checkout / ".git").write_text("gitdir: metadata\n")
        (tmp_path / ".git").mkdir()
    else:
        exists = Path.exists

        def without_git(path: Path, *, follow_symlinks: bool = True) -> bool:
            if path.name == ".git" and path.parent in source.parents:
                return False
            return exists(path, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "exists", without_git)

    def distribution(name: str) -> SimpleNamespace:
        assert name == "tbhbdev"
        if not present:
            raise build_info.metadata.PackageNotFoundError

        def read_text(name: str) -> str:
            assert name == "direct_url.json"
            return '{"vcs_info": {"commit_id": "' + "b" * 40 + '"}}'

        return SimpleNamespace(read_text=read_text)

    def output(command: list[str], **kwargs: object) -> str:
        assert command == ["git", "-C", str(checkout), "rev-parse", "HEAD"]
        assert kwargs == {"text": True, "stderr": subprocess.DEVNULL, "timeout": 5}
        return "a" * 40 + "\n"

    monkeypatch.setattr(build_info.metadata, "distribution", distribution)
    monkeypatch.setattr(subprocess, "check_output", output)
    return {
        "tools_commit": "a" * 40 if present else "unknown",
        "wheel_commit": "b" * 40 if present else "unknown",
    }


def make_run(tmp_path: Path, **changes: object) -> state.Run:
    files = runfiles.RunFiles(tmp_path / "runs", "example")
    value = run_request(**changes)
    home = runfiles.private_dir(tmp_path / "home")
    tmpdir = runfiles.private_dir(tmp_path / "private-tmp")
    return state.Run(
        files,
        value,
        host_settings(),
        host_settings().hosts[0],
        home,
        tmpdir,
        1000,
        time.monotonic() + 10,
    )


def bounded_file_reads(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    wrapped = os.fdopen

    @contextmanager
    def checked(fd: int, mode: str) -> Generator[SimpleNamespace]:
        assert mode == "rb"
        with wrapped(fd, mode) as stream:

            def read(count: int) -> bytes:
                assert count == size
                return stream.read(count)

            yield SimpleNamespace(fileno=stream.fileno, read=read)

    monkeypatch.setattr(os, "fdopen", checked)
