"""The push guard checks names when a push creates a branch."""

from __future__ import annotations

import io
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev import userconfig
from tbhbdev.alpha import pre_push_guard as pg

if TYPE_CHECKING:
    from pathlib import Path


ZERO = "0" * 40
BASE = "1" * 40
HEAD = "2" * 40
BRANCH = "refs/heads/agent/prepare-EXAMPLEHOST01"


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            f"HEAD {HEAD} {BRANCH} {ZERO}",
            "push refused because the branch name contains a host name",
        ),
        (f"HEAD {HEAD} {BRANCH} {BASE}", ""),
        (f"HEAD {ZERO} {BRANCH} {BASE}", ""),
        (f"HEAD {HEAD} refs/tags/prepare-examplehost01 {ZERO}", ""),
    ],
)
def test_push_checks_host_names_when_creating_a_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    row: str,
    message: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = userconfig.path()
    config.parent.mkdir()
    config.write_text(
        '[[agent.host]]\nname = "examplehost01"\nos = "linux"\nshell = "bash"\n'
        'account = "agent"\naccounts = ["agent"]\nproven_harnesses = ["codex"]\n'
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(row + "\n"))
    monkeypatch.setattr(pg, "remote_tips", Mock(return_value=[]))
    verify_ref = Mock(return_value=[])
    monkeypatch.setattr(pg, "ref_problems", verify_ref)

    assert pg.main(["origin", "../remote.git"], "pre-push-guard") == bool(message)
    assert capsys.readouterr().err == (f"{message}\n" if message else "")
    assert verify_ref.call_count == int(not message)
