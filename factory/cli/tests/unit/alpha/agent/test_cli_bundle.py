"""Check that the gather step packs the installed CLI and pins its dependencies."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from importlib.metadata import PathDistribution
from typing import TYPE_CHECKING
from unittest.mock import Mock

from tbhbdev.alpha.agent import cli_bundle

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

COMMIT = "0123456789abcdef0123456789abcdef01234567"
WHEEL = "https://example.test/telemetry-0-py3-none-any.whl"


def write_installation(root: Path) -> list[PathDistribution]:
    files = {
        "tbhbdev/__init__.py": b"",
        "tbhbdev/cli.py": b"# CLI\n",
        "tbhbdev/__pycache__/cli.cpython-315.pyc": b"cache",
        "tbhbdev-0.dist-info/METADATA": b"Name: tbhbdev\nVersion: 0\n",
        "tbhbdev-0.dist-info/direct_url.json": json.dumps(
            {"url": "https://example.test/tbhbdev", "vcs_info": {"commit_id": COMMIT}}
        ).encode(),
        "plain-1.0.dist-info/METADATA": b"Name: plain\nVersion: 1.0\n",
        "telemetry-0.dist-info/METADATA": b"Name: telemetry\nVersion: 0\n",
        "telemetry-0.dist-info/direct_url.json": json.dumps(
            {"url": WHEEL, "archive_info": {}}
        ).encode(),
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return [
        PathDistribution(root / "tbhbdev-0.dist-info"),
        PathDistribution(root / "plain-1.0.dist-info"),
        PathDistribution(root / "telemetry-0.dist-info"),
    ]


def test_installed_packs_the_recorded_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distributions = write_installation(tmp_path)
    monkeypatch.setattr(cli_bundle, "PACKAGE", tmp_path / "tbhbdev")
    monkeypatch.setattr(
        cli_bundle.metadata, "distribution", Mock(return_value=distributions[0])
    )
    monkeypatch.setattr(cli_bundle.metadata, "distributions", lambda: distributions)
    bundle, commit, digest = cli_bundle.installed()
    assert commit == COMMIT
    assert digest == hashlib.sha256(bundle).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:") as archive:
        assert archive.getnames() == [
            "requirements.txt",
            "tbhbdev-0.dist-info/METADATA",
            "tbhbdev-0.dist-info/direct_url.json",
            "tbhbdev/__init__.py",
            "tbhbdev/cli.py",
        ]
        payload = archive.extractfile("requirements.txt")
        assert payload is not None
        with payload:
            assert payload.read() == f"plain==1.0\ntelemetry @ {WHEEL}\n".encode()
