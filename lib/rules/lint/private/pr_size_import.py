# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Find unchanged copies from the public source commit named by an import map."""

# ruff: noqa: INP001 -- This is a standalone script in the counter's runfiles.

import os
import re
import subprocess  # nosec B404
import sys
import tomllib
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import TypeGuard, cast


def clean_path(value: object) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and not path.is_absolute()
        and str(path) == value
        and not any(part.lower() in (".git", "..") for part in path.parts)
    )


def read_map(data: bytes) -> tuple[str, str, dict[str, str]]:
    made = tomllib.loads(data.decode())
    repo, commit, dest = (made.get(key) for key in ("repo", "commit", "dest"))
    copies = made.get("copy", {})
    if (
        not isinstance(repo, str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+", repo)
        or repo.split("/")[1] in (".", "..")
        or not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not clean_path(dest)
        or not isinstance(copies, dict)
    ):
        return "", "", {}
    writes = {
        path: src
        for src, path in cast("dict[object, object]", copies).items()
        if clean_path(src) and clean_path(path) and path.startswith(f"{dest}/")
    }
    if len(writes) != len(copies):
        return "", "", {}
    return repo, commit, writes


def copied_paths(
    writes: dict[str, str], source: dict[str, bytes], candidate: dict[str, bytes]
) -> set[str]:
    return {
        path
        for path, src in writes.items()
        if path in candidate and candidate[path] == source.get(src)
    }


def git(executable: str, *args: str, cwd: str | None = None) -> bytes:
    return subprocess.run(  # noqa: S603  # nosec B603
        [executable, "--no-replace-objects", "-c", "credential.helper=", *args],
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


def tree(executable: str, commit: str, cwd: str | None = None) -> dict[str, bytes]:
    listing = git(executable, "ls-tree", "-rz", "--full-tree", commit, cwd=cwd)
    found = {}
    for record in listing.split(b"\0")[:-1]:
        blob, path = record.split(b"\t", 1)
        if blob.split()[1] == b"blob":
            found[os.fsdecode(path)] = blob
    return found


def source_tree(executable: str, repo: str, commit: str) -> dict[str, bytes]:
    with TemporaryDirectory() as scratch:
        git(executable, "init", "--bare", "--object-format=sha1", cwd=scratch)
        git(
            executable,
            "fetch",
            "--depth=1",
            "--no-tags",
            "--no-recurse-submodules",
            f"https://github.com/{repo}.git",
            commit,
            cwd=scratch,
        )
        return tree(executable, commit, scratch)


def main(executable: str, head: str, changed_file: str) -> None:
    candidate = tree(executable, head)
    verified: set[str] = set()
    for raw in sys.stdin.buffer.read().split(b"\0")[:-1]:
        path = os.fsdecode(raw)
        if not candidate.get(path, b"").startswith(b"100"):
            continue
        data = git(executable, "cat-file", "blob", f"{head}:{path}")
        try:
            repo, commit, writes = read_map(data)
            if writes:
                verified |= copied_paths(
                    writes, source_tree(executable, repo, commit), candidate
                )
        except subprocess.SubprocessError, ValueError, OSError:
            # A malformed map or unavailable frozen source proves no copies.
            continue
    changed = Path(changed_file)
    paths = changed.read_bytes().split(b"\0")[:-1]
    with changed.open("wb") as remaining:
        for raw in paths:
            output = sys.stdout.buffer if os.fsdecode(raw) in verified else remaining
            output.write(raw + b"\0")


if __name__ == "__main__":
    main(*sys.argv[1:])
