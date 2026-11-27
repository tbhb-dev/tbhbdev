import hashlib
import json
import subprocess
import sys
from pathlib import Path
from stat import S_IMODE

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_backup_playbook_stages_private_programs_and_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ansible = ROOT / "ansible"
    staging = tmp_path / "staging"
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(
        "ca_backup_hosts:\n  hosts:\n    localhost:\n", encoding="utf-8"
    )
    monkeypatch.setenv("ANSIBLE_CONFIG", str(ansible / "ansible.cfg"))
    monkeypatch.setenv("ANSIBLE_LOCAL_TEMP", str(tmp_path / "ansible"))
    overrides = {
        "ansible_connection": "local",
        "ansible_python_interpreter": sys.executable,
        "ansible_remote_tmp": str(tmp_path / "remote"),
        "ca_backup_python": sys.executable,
        "ca_backup_staging": str(staging),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ansible.cli.playbook",
            "-i",
            str(inventory),
            str(ansible / "playbooks/ca-backup.yml"),
            "-e",
            json.dumps(overrides),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert S_IMODE(staging.stat().st_mode) == 0o700
    assert {path.name for path in staging.iterdir()} == {
        "backup.py",
        "status.py",
        "SHA256SUMS",
    }
    expected_manifest = []
    for name in ("backup.py", "status.py"):
        source = (ROOT / "apps/ca/backup" / name).read_bytes()
        assert (staging / name).read_bytes() == source
        expected_manifest.append(f"{hashlib.sha256(source).hexdigest()}  {name}\n")
    assert (staging / "SHA256SUMS").read_text(encoding="ascii") == "".join(
        expected_manifest
    )
    assert {S_IMODE(path.stat().st_mode) for path in staging.iterdir()} == {0o600}
    uid = tmp_path.stat().st_uid
    assert staging.stat().st_uid == uid
    assert {path.stat().st_uid for path in staging.iterdir()} == {uid}
