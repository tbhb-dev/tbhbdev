"""Check the CLI archive format and the build identities it accepts."""

import hashlib
import io
import tarfile

import pytest

from tbhbdev.alpha.agent.decisions.cli_bundle import pack
from tbhbdev.alpha.agent.model import Invalid

COMMIT = "0123456789abcdef0123456789abcdef01234567"
PACKAGE = {"tbhbdev/__init__.py": b"", "tbhbdev/cli.py": b"# CLI\n"}
DIST_INFO = {
    "tbhbdev-0.dist-info/METADATA": b"Name: tbhbdev\nVersion: 0\n",
    "tbhbdev-0.dist-info/direct_url.json": b'{"vcs_info":{"commit_id":"'
    + COMMIT.encode()
    + b'"}}',
}
PINS = {"zeta": "2.0", "alpha": "1.0", "tbhbdev": "0"}


def test_deterministic_bundle() -> None:
    expected = pack(PACKAGE, DIST_INFO, identity=COMMIT, distributions=PINS)
    assert pack(PACKAGE, DIST_INFO, identity=COMMIT, distributions=PINS) == expected
    assert (
        pack(
            dict(reversed(PACKAGE.items())),
            dict(reversed(DIST_INFO.items())),
            identity=COMMIT,
            distributions=dict(reversed(PINS.items())),
        )
        == expected
    )
    bundle, digest = expected
    assert hashlib.sha256(bundle).hexdigest() == digest


def test_bundle_contents() -> None:
    package = {
        **PACKAGE,
        "tbhbdev/__pycache__/cli.cpython-315.pyc": b"cache",
        "tbhbdev/sub/__pycache__/other": b"cache",
        "tbhbdev/old.pyc": b"cache",
    }
    bundle, _ = pack(package, DIST_INFO, identity=COMMIT, distributions=PINS)
    expected = {**PACKAGE, **DIST_INFO, "requirements.txt": b"alpha==1.0\nzeta==2.0\n"}
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:") as archive:
        assert archive.getnames() == sorted(expected)
        for entry in archive:
            assert (entry.mtime, entry.uid, entry.gid) == (0, 0, 0)
            assert (entry.uname, entry.gname, entry.mode) == ("", "", 0o644)
            payload = archive.extractfile(entry)
            assert payload is not None
            with payload:
                assert payload.read() == expected[entry.name]


@pytest.mark.parametrize(
    "identity", ["unknown", "editable /some/path", "abc123", "a" * 64]
)
def test_refuses_unidentified_build(identity: str) -> None:
    with pytest.raises(Invalid, match="40-hex"):
        pack(PACKAGE, DIST_INFO, identity=identity, distributions=PINS)
