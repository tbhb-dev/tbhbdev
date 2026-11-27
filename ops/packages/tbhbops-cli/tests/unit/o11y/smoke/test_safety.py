"""Smoke path checks and evidence scans."""

import stat
from pathlib import Path

import pytest
from tbhbops_cli.o11y.smoke import (
    safety,
)
from test_o11y_cli import Fake
from unit.o11y.smoke.runtime_fixture import OUT


def test_scan_names_only_and_binary() -> None:
    fs = Fake()
    fs.write_atomic(Path("/candidate"), b"\xff\x00TOKEN\xfe", 0o600)
    with pytest.raises(safety.SmokeError, match=r"^sentinel secret: candidate$"):
        safety.scan_path(fs, Path("/candidate"), (b"TOKEN",))
    fs.write_atomic(Path("/candidate"), b"\xff\x00clean\xfe", 0o600)
    safety.scan_path(fs, Path("/candidate"), (b"TOKEN",))


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "empty-file",
        "empty-dir",
        "fifo",
        "nested-link",
        "nested-empty",
        "nested-binary",
    ],
)
def test_scan_fail_closed_tree(kind: str) -> None:
    fs = Fake()
    if kind == "empty-file":
        fs.write_atomic(OUT, "", 0o600)
    elif kind == "fifo":
        fs.modes[OUT] = stat.S_IFIFO
    elif kind != "missing":
        fs.mkdir(OUT)
        if kind.startswith("nested"):
            fs.mkdir(OUT / "sub")
            fs.write_atomic(OUT / "clean", "clean", 0o600)
            fs.write_atomic(OUT / "sub/empty", "", 0o600)
            if kind == "nested-link":
                fs.modes[OUT / "sub/link"] = stat.S_IFLNK
            elif kind == "nested-binary":
                fs.write_atomic(OUT / "sub/binary", b"\xffTOKEN\xfe", 0o600)
    if kind == "nested-empty":
        safety.scan_path(fs, OUT, (b"TOKEN",))
    else:
        with pytest.raises(safety.SmokeError):
            safety.scan_path(fs, OUT, (b"TOKEN",))


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("missing", "scan unavailable: candidate"),
        ("fifo", "scan unavailable: candidate"),
        ("empty", "scan empty: candidate"),
    ],
)
def test_scan_error_wording(kind: str, expected: str) -> None:
    fs = Fake()
    path = Path("/candidate")
    if kind == "fifo":
        fs.modes[path] = stat.S_IFIFO
    elif kind == "empty":
        fs.write_atomic(path, b"", 0o600)
    with pytest.raises(safety.SmokeError) as failure:
        safety.scan_path(fs, path, (b"secret",))
    assert str(failure.value) == expected
