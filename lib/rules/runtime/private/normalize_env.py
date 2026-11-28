# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:words codesign followlinks libpython sysconfigdata

"""Remove repository paths from a uv environment's Bazel inputs."""

import base64
import csv
import hashlib
import os
import re
import subprocess  # nosec B404
import sys
from fnmatch import fnmatchcase
from io import StringIO
from pathlib import Path

# Match the bin exclusions in the generated BUILD.
BIN_EXCLUDES = ("activate*", "deactivate*", "python*", "*.ps1")


def normalize_scripts(bin_dir: Path) -> dict[str, tuple[str, str]]:
    prefix = os.fsencode(bin_dir) + b"/"
    executable = rb"(python[0-9.]*t?)"
    plain = re.compile(rb"\A#!" + re.escape(prefix) + executable + rb"(?=\r?\n|\Z)")
    quoted = re.escape(prefix.replace(b"'", b"""'"'"'"""))
    # uv uses a shell wrapper when the interpreter path is too long or has spaces.
    wrapped = re.compile(
        rb"\A#!/bin/sh\n'''exec' '"
        + quoted
        + executable
        + rb"""' "\$0" "\$@"\n' '''(?=\r?\n|\Z)"""
    )
    shebang = rb"#!/usr/bin/env \1"

    records = {}
    for path in bin_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        original = path.read_bytes()
        if b"\x00" in original:
            continue
        data = wrapped.sub(shebang, plain.sub(shebang, original))
        if data != original:
            path.write_bytes(data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode()
            records[f"../../../bin/{path.name}"] = (
                f"sha256={digest.rstrip('=')}",
                str(len(data)),
            )
    return records


def update_records(site_packages: Path, scripts: dict[str, tuple[str, str]]) -> None:
    # uv records the hash and length of each generated launcher in its wheel
    # metadata. The old hashes also differ between output bases.
    for path in site_packages.glob("*.dist-info/RECORD"):
        with path.open(encoding="utf-8", newline="") as record:
            rows = list(csv.reader(record))
        changed = False
        for row in rows:
            if row[0] in scripts:
                row[1:] = scripts[row[0]]
                changed = True
        if changed:
            output = StringIO()
            csv.writer(output, lineterminator="\n").writerows(rows)
            path.write_text(output.getvalue(), encoding="utf-8")


def normalize_dylib(path: Path) -> None:
    # uv sets LC_ID_DYLIB to the absolute path on macOS. Restore a
    # relocatable ID and refresh the arm64 ad hoc signature.
    subprocess.run(  # nosec B603
        ["/usr/bin/install_name_tool", "-id", f"@rpath/{path.name}", path], check=True
    )
    subprocess.run(  # nosec B603
        ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", path],
        check=True,
    )


def check_paths(root: Path, bin_dir: Path, install: Path) -> None:
    paths = [
        path
        for path in bin_dir.iterdir()
        if not any(fnmatchcase(path.name, pattern) for pattern in BIN_EXCLUDES)
    ]
    for directory, subdirs, files in os.walk(install, followlinks=True):
        subdirs[:] = [name for name in subdirs if name != "__pycache__"]
        paths.extend(Path(directory) / name for name in files)
    prefix = os.fsencode(root)
    for path in paths:
        if path.is_file() and prefix in path.read_bytes():
            name = path.relative_to(root).as_posix()
            message = f"{name} contains the absolute repository path"
            raise ValueError(message)


def normalize(root: Path, install: Path, site_packages: Path) -> None:
    bin_dir = root / "venv/bin"
    update_records(site_packages, normalize_scripts(bin_dir))

    for path in install.glob("lib/python*/_sysconfigdata__*.py"):
        data = path.read_bytes()
        path.write_bytes(data.replace(os.fsencode(install), b"/install"))
    prefix = os.fsencode(root)
    for path in install.glob("lib/libpython*.dylib"):
        if prefix in path.read_bytes():
            normalize_dylib(path)
    check_paths(root, bin_dir, install)


if __name__ == "__main__":
    normalize(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
