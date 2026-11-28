# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:words codesign libpython sysconfigdata

import base64
import hashlib
import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import call

import pytest

from runtime.private import normalize_env

if TYPE_CHECKING:
    from pathlib import Path


def test_generated_inputs_match_across_repository_paths(tmp_path: Path) -> None:
    roots = [tmp_path / "first", tmp_path / "second output base's root"]
    outputs = []
    for root in roots:
        install = root / "python/cpython"
        stdlib = install / "lib/python3.15"
        stdlib.mkdir(parents=True)
        sysconfig = stdlib / "_sysconfigdata__example.py"
        sysconfig.write_bytes(
            b"build_time_vars = {'prefix': '" + os.fsencode(install) + b"'}\n"
        )
        scripts = root / "venv/bin"
        scripts.mkdir(parents=True)
        interpreter = os.fsencode(scripts) + b"/python"
        plain = scripts / "plain"
        plain.write_bytes(b"#!" + interpreter + b"3.15\nprint('plain')\n")
        wrapped = scripts / "wrapped"
        quoted = interpreter.replace(b"'", b"""'"'"'""")
        wrapped.write_bytes(
            b"#!/bin/sh\n'''exec' '" + quoted + b"' \"$0\" \"$@\"\n' '''\n"
        )
        native = scripts / "native"
        native.write_bytes(b"\x7fELF\x00\xff")
        site_packages = root / "venv/lib/python3.15/site-packages"
        record = site_packages / "example.dist-info/RECORD"
        record.parent.mkdir(parents=True)
        original = plain.read_bytes()
        record.write_text(
            f"../../../bin/plain,sha256={hashlib.sha256(original).hexdigest()},{len(original)}\n"
            "example.dist-info/RECORD,,\n",
            encoding="utf-8",
        )
        (scripts / "activate").write_bytes(os.fsencode(root))
        (scripts / "directory").mkdir()
        pycache = stdlib / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.pyc").write_bytes(os.fsencode(root))
        untouched = site_packages / "unrelated.dist-info/RECORD"
        untouched.parent.mkdir()
        untouched.write_bytes(b"unrelated.dist-info/RECORD,,\n")

        normalize_env.normalize(root, install, site_packages)
        files = (plain, wrapped, native, sysconfig, record, untouched)
        outputs.append([path.read_bytes() for path in files])

    assert outputs[0] == outputs[1]
    assert all(os.fsencode(root) not in data for root in roots for data in outputs[0])
    assert outputs[0][:4] == [
        b"#!/usr/bin/env python3.15\nprint('plain')\n",
        b"#!/bin/sh\n'''exec' python \"$0\" \"$@\"\n' '''\n",
        b"\x7fELF\x00\xff",
        b"build_time_vars = {'prefix': '/install'}\n",
    ]
    data = outputs[0][0]
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    assert outputs[0][4] == (
        b"../../../bin/plain,sha256="
        + digest
        + f",{len(data)}\n".encode()
        + b"example.dist-info/RECORD,,\n"
    )
    assert outputs[0][5] == b"unrelated.dist-info/RECORD,,\n"


@pytest.mark.parametrize("name", ["venv/bin/native", "python/cpython/lib/extension.so"])
def test_rejects_paths_in_binary_inputs(tmp_path: Path, name: str) -> None:
    (tmp_path / "venv/bin").mkdir(parents=True)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\x00" + os.fsencode(tmp_path))
    with pytest.raises(
        ValueError, match=f"^{name} contains the absolute repository path$"
    ):
        normalize_env.normalize(
            tmp_path, tmp_path / "python/cpython", tmp_path / "site-packages"
        )


def test_normalizes_only_the_patched_dylib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "venv/bin").mkdir(parents=True)
    install = tmp_path / "python/cpython"
    lib = install / "lib"
    lib.mkdir(parents=True)
    patched = lib / "libpython3.15.dylib"
    patched.write_bytes(b"\xff\x00" + os.fsencode(patched))
    clean = lib / "libpython3.dylib"
    clean.write_bytes(b"\xff\x00")
    commands = []

    def run(*args: object, **kwargs: object) -> None:
        commands.append(call(*args, **kwargs))
        patched.write_bytes(b"\xff\x00@rpath/libpython3.15.dylib")

    monkeypatch.setattr(subprocess, "run", run)

    normalize_env.normalize(tmp_path, install, tmp_path / "site-packages")

    assert commands == [
        call(
            [
                "/usr/bin/install_name_tool",
                "-id",
                "@rpath/libpython3.15.dylib",
                patched,
            ],
            check=True,
        ),
        call(
            [
                "/usr/bin/codesign",
                "--force",
                "--sign",
                "-",
                "--timestamp=none",
                patched,
            ],
            check=True,
        ),
    ]
