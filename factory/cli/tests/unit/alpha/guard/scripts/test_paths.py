# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise path expansion without reading the filesystem."""

import os
from pathlib import Path

import pytest

from tbhbdev.alpha.guard.scripts.paths import TMP_ROOT, Place, normalize


@pytest.fixture
def home() -> Path:
    return Path("/example/home")


def places(home: Path, tmpdir: str = "") -> Place:
    return Place(str(home / "work"), normalize(str(home)), tmpdir)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("~", "{h}"),
        ("~/a", "{h}/a"),
        ("$HOME", "{h}"),
        ("$HOME/a", "{h}/a"),
        ("${HOME}", "{h}"),
        ("${HOME}/a/../b", "{h}/b"),
        ("~x/a", "{w}/~x/a"),
        ("$HOME2/a", "{w}/$HOME2/a"),
        ("${HOME}x/a", "{w}/$" + "{HOME}x/a"),
        ("a/b.sh", "{w}/a/b.sh"),
        ("./a/../b.sh", "{w}/b.sh"),
        ("../b.sh", "{h}/b.sh"),
        ("/abs/../x", "/x"),
        ("/abs//x/./y", "/abs/x/y"),
        ("~/../x", "{h}/../x"),
        ("/private/tmp/a", f"{TMP_ROOT}/a"),
        ("/private/tmp", TMP_ROOT),
        ("/private/tmp2/a", "/private/tmp2/a"),
        ("/private/var/a", "/private/var/a"),
        (f"{TMP_ROOT}/a", f"{TMP_ROOT}/a"),
        ("/a/private/tmp/b", "/a/private/tmp/b"),
        ("/private/tmp/../a", "/private/a"),
        (f"{TMP_ROOT}/../private/tmp/a", f"{TMP_ROOT}/a"),
    ],
)
def test_resolve(home: Path, raw: str, expected: str) -> None:
    place = places(home)
    want = expected.replace("{h}", place.home).replace("{w}", place.cwd)
    assert place.resolve(raw) == os.path.normpath(want)


def test_normalize() -> None:
    assert normalize("a/b") == "/a/b"
    assert normalize("a/../..") == "/"
    assert normalize("b", "/x/y") == "/x/y/b"
    assert normalize("/b", "/x/y") == "/b"
    assert normalize("", "/x/y") == "/x/y"
    assert normalize("/private/tmp/x") == f"{TMP_ROOT}/x"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("//a", "/a"),
        ("///a", "/a"),
        ("//", "/"),
        ("//private/tmp/a", f"{TMP_ROOT}/a"),
        ("//a//b", "/a/b"),
        ("/Xa", "/Xa"),
        ("//Xa", "/Xa"),
    ],
)
def test_leading_slashes_collapse(raw: str, expected: str) -> None:
    assert normalize(raw) == expected
    assert normalize(raw, "/x") == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$TMPDIR", f"{TMP_ROOT}/run-1"),
        ("$TMPDIR/a", f"{TMP_ROOT}/run-1/a"),
        ("${TMPDIR}", f"{TMP_ROOT}/run-1"),
        ("${TMPDIR}/a/../b", f"{TMP_ROOT}/run-1/b"),
        ("$TMPDIR2/a", "{w}/$TMPDIR2/a"),
        ("${TMPDIR}x/a", "{w}/$" + "{TMPDIR}x/a"),
        ("x/$TMPDIR", "{w}/x/$TMPDIR"),
        ("$HOME/a", "{h}/a"),
    ],
)
def test_resolve_expands_tmpdir(home: Path, raw: str, expected: str) -> None:
    place = places(home, f"{TMP_ROOT}/run-1")
    want = expected.replace("{h}", place.home).replace("{w}", place.cwd)
    assert place.resolve(raw) == os.path.normpath(want)


def test_resolve_leaves_an_empty_tmpdir_literal(home: Path) -> None:
    place = places(home, "")
    assert place.resolve("$TMPDIR/a") == f"{place.cwd}/$TMPDIR/a"
    assert place.resolve("${TMPDIR}") == f"{place.cwd}/$" + "{TMPDIR}"
