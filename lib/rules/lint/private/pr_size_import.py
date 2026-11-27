# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Find verified copies and rewrites of the source pinned by an import map."""

# ruff: noqa: INP001 -- This is a standalone script in the counter's runfiles.

import os
import re
import subprocess  # nosec B404
import sys
import tomllib
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import TypeGuard, cast

Replacements = list[tuple[bytes, bytes]]
PAIR_SIZE = 2


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


def replacement_pairs(value: object) -> Replacements:
    if not isinstance(value, list) or not all(
        isinstance(pair, list)
        and len(pair) == PAIR_SIZE
        and all(isinstance(item, str) for item in cast("list[object]", pair))
        for pair in cast("list[object]", value)
    ):
        message = "rewrite replacements must be a list of [old, new] strings"
        raise ValueError(message)
    return [(old.encode(), new.encode()) for old, new in cast("list[list[str]]", value)]


def read_map(data: bytes) -> tuple[str, str, dict[str, tuple[str, Replacements]]]:
    made = tomllib.loads(data.decode())
    repo, commit, dest = (made.get(key) for key in ("repo", "commit", "dest"))
    copies = made.get("copy", {})
    rewrites = made.get("rewrite", {})
    if (
        not isinstance(repo, str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+", repo)
        or repo.split("/")[1] in (".", "..")
        or not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not clean_path(dest)
        or not isinstance(copies, dict)
        or not isinstance(rewrites, dict)
    ):
        return "", "", {}
    writes: dict[str, tuple[str, Replacements]] = {
        path: (src, [])
        for src, path in cast("dict[object, object]", copies).items()
        if clean_path(src) and clean_path(path) and path.startswith(f"{dest}/")
    }
    if len(writes) != len(copies):
        return "", "", {}
    for src, entry in cast("dict[object, object]", rewrites).items():
        if not clean_path(src) or src in copies or not isinstance(entry, dict):
            return "", "", {}
        path = entry.get("dest")
        if not clean_path(path) or not path.startswith(f"{dest}/") or path in writes:
            return "", "", {}
        writes[path] = src, replacement_pairs(entry.get("replace", []))
    return repo, commit, writes


def rewrite_bytes(data: bytes, pairs: Replacements) -> bytes | None:
    # Match monorepo verify: apply each replacement to the result of the last.
    for old, new in pairs:
        if old not in data:
            return None
        data = data.replace(old, new)
    return data


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


def copied_paths(
    executable: str,
    repo: str,
    commit: str,
    writes: dict[str, tuple[str, Replacements]],
    candidate: dict[str, bytes],
) -> set[str]:
    found: set[str] = set()
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
        source = tree(executable, commit, scratch)
        for path, (src, pairs) in writes.items():
            if path not in candidate or src not in source:
                continue
            if not pairs:
                if candidate[path] == source[src]:
                    found.add(path)
                continue
            src_mode, _, src_oid = source[src].split()
            dst_mode, _, dst_oid = candidate[path].split()
            if src_mode != dst_mode:
                continue
            data = git(executable, "cat-file", "blob", src_oid.decode(), cwd=scratch)
            wanted = rewrite_bytes(data, pairs)
            if wanted is not None and wanted == git(
                executable, "cat-file", "blob", dst_oid.decode()
            ):
                found.add(path)
    return found


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
                verified |= copied_paths(executable, repo, commit, writes, candidate)
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
