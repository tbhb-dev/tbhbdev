# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Stub executables and a temporary account for tests that use host tools."""

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from tbhbdev import host_executables
from tbhbdev.secrets.config import TEST_TOKEN_ENV

type Json = dict[str, object]

STUBTOOL = Path(__file__).resolve().parent / "stubtool.py"
STUB_TOOLS = (
    "chmod",
    "dscl",
    "getent",
    "getfacl",
    "gh",
    "git",
    "groupadd",
    "id",
    "install",
    "ls",
    "mise",
    "op",
    "security",
    "setfacl",
    "ssh",
    "sudo",
    "swift",
    "tailscale",
    "useradd",
    "usermod",
)
# The system tools the host scripts, the stubs and the exec tests run, linked
# beside the stubs so PATH holds nothing else, and no real tailscale, op or
# ssh can run. The chmod stub runs the real chmod for a plain mode change.
SYSTEM_TOOLS = (
    "cat",
    "cmp",
    "echo",
    "find",
    "mkdir",
    "mktemp",
    "mv",
    "rm",
    "sh",
    "stat",
    "true",
)


@dataclass
class Stubs:
    """The stub executables' directory, scenario state and call log."""

    dir: Path
    bin: Path

    def state(self) -> Json:
        path = self.dir / "state.json"
        return cast("Json", json.loads(path.read_text())) if path.exists() else {}

    def set(self, **values: object) -> None:
        state = self.state()
        state.update(values)
        (self.dir / "state.json").write_text(json.dumps(state))

    def calls(self, tool: str | None = None) -> list[Json]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        entries = [
            cast("Json", json.loads(line)) for line in path.read_text().splitlines()
        ]
        return [e for e in entries if tool is None or e["tool"] == tool]

    def argvs(self, tool: str) -> list[list[str]]:
        return [cast("list[str]", e["argv"]) for e in self.calls(tool)]

    def home(self, host: str = "local") -> Path:
        return self.dir / "homes" / host / "agent"


def write_stub(launcher: Path, tool: str) -> None:
    """Write an executable that runs stubtool.main(tool, argv) under -I.

    The launcher is a /bin/sh script that execs the interpreter, because
    Linux refuses a script whose first line passes 256 bytes, and a
    `#!{sys.executable}` line does in a deep sandbox.
    """
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(STUBTOOL.parent)!r})\n"
        "import stubtool\n"
        f"stubtool.main({tool!r}, sys.argv[1:])\n"
    )
    launcher.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} -I -c {shlex.quote(code)} "$@"\n'
    )
    launcher.chmod(0o755)


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stubs:
    """Stub tools alone on PATH, HOME in tmp_path, and no token override."""
    stub_dir = tmp_path / "stub"
    bin_dir = stub_dir / "bin"
    bin_dir.mkdir(parents=True)
    for tool in STUB_TOOLS:
        write_stub(bin_dir / tool, tool)
    for tool in SYSTEM_TOOLS:
        found = shutil.which(tool, path=f"/usr/bin{os.pathsep}/bin")
        assert found is not None, tool
        (bin_dir / tool).symlink_to(found)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(host_executables, "search", lambda: str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("STUB_DIR", str(stub_dir))
    monkeypatch.delenv(TEST_TOKEN_ENV, raising=False)
    return Stubs(stub_dir, bin_dir)
