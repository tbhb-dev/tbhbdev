"""Check the read-back's explicit schema defaults without connecting to Windows."""

import shlex
from pathlib import Path


def test_read_back_defaults_only_the_omitted_schema_values() -> None:
    role = next(
        path / "ansible" / "roles" / "windows_update_policy"
        for path in Path(__file__).resolve().parents
        if (path / "ansible" / "roles").is_dir()
    )
    script = (role / "files" / "read-back.ps1").read_text(encoding="utf-8")
    calls = [
        shlex.split(line.strip())
        for line in script.splitlines()
        if line.strip().startswith("Confirm-Text ")
    ]
    defaults = {call[1]: tuple(call[2:]) for call in calls if len(call) == 4}
    assert defaults == {
        "Principals/t:Principal/t:RunLevel": ("LeastPrivilege", "LeastPrivilege"),
        "Settings/t:Enabled": ("true", "true"),
        "Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd": ("false", "false"),
        "Triggers/t:$type/t:Enabled": ("true", "true"),
    }
