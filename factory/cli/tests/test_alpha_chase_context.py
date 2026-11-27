"""Tests for the coordinator's stale pull request hook."""

import io
import json
import os
import subprocess
import sys
from time import time
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import chase_context

if TYPE_CHECKING:
    from pathlib import Path

NOW = time()
OPS = "CHASE  ops#420 573366d3 33m draft=false x | a"
TOOLS = "CHASE  tools#258 33fda702 95m draft=false x | b"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def unexpected_scan(*_args: object, **_kwargs: object) -> None:
        raise AssertionError

    monkeypatch.setattr(chase_context, "time", lambda: NOW)
    monkeypatch.setattr(chase_context.subprocess, "run", unexpected_scan)
    monkeypatch.setenv("HOME", str(tmp_path))


def prompt(home: Path, session: str, monkeypatch: pytest.MonkeyPatch) -> None:
    data = {"cwd": str(home / "Code/github.com/tbhb-dev"), "session_id": session}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))


def test_cached_message_is_injected_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "st"
    monkeypatch.setenv("TBHB_CHASE_STATE", str(state))
    cache = tmp_path / ".local/state/tbhb/chase-context.cache"
    cache.parent.mkdir(parents=True)
    older_ops = OPS.replace("33m", "38m")
    older_tools = TOOLS.replace("95m", "100m")
    updated = TOOLS.replace("33fda702 95m", "aaaaaaaa 1m")
    cases: list[tuple[str, list[str], int | None, bool]] = [
        ("s1", [OPS, TOOLS], None, True),
        ("s1", [older_tools, older_ops], None, False),
        ("s2", [older_ops, older_tools], None, True),
        ("s1", [older_ops, updated], None, True),
        ("s1", [older_ops], None, True),
        ("s1", [older_ops], 25 * 60, True),
        ("s1", [older_ops], None, False),
        ("s1", [], None, False),
    ]
    for sid, rows, age, prints in cases:
        message = "\n".join(["WATCHDOG: head", *rows]) if rows else ""
        cache.write_text(message)
        os.utime(cache, (NOW, NOW))
        if age is not None:
            os.utime(state / sid, (NOW - age, NOW - age))
        prompt(tmp_path, sid, monkeypatch)
        assert alpha.main(["chase-context"], "tbhbdev alpha") == 0
        assert capsys.readouterr() == (f"{message}\n" if prints else "", "")


def test_refresh_filters_rows_and_uses_the_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = str(tmp_path / "scanner")
    monkeypatch.setenv("TBHB_STALE_PRS", helper)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [
        "unrelated line",
        "CHASE ops#1 abc 1440m title",
        "CHASE ops#2 abc 33m renovate",
        "CHASE ops#3 abc",
        OPS,
    ]

    def scanner(
        argv: list[str], *, env: dict[str, str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        assert argv == ["timeout", "40", helper, "20"]
        assert env["PATH"].startswith(f"{tmp_path / 'tbhbdev' / 'agent-bin'}:")
        return subprocess.CompletedProcess(argv, 0, "\n".join(rows))

    monkeypatch.setattr(chase_context.subprocess, "run", scanner)
    monkeypatch.setenv("TBHB_CHASE_STATE", "")
    state = tmp_path / ".local/state/tbhb"
    cache = state / "chase-context.cache"
    cache.parent.mkdir(parents=True)
    cache.write_text("stale cache")
    os.utime(cache, (NOW - 301, NOW - 301))
    prompt(tmp_path, "/!?", monkeypatch)
    assert alpha.main(["chase-context"], "tbhbdev alpha") == 0
    message = f"{chase_context.WATCHDOG}\n{rows[3]}\n{OPS}"
    assert capsys.readouterr() == (f"{message}\n", "")
    assert cache.read_text() == message
    assert (state / "chase-context.state/none").is_file()


def test_skips_other_workspaces_and_zero_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    budget = tmp_path / ".local/state/tbhbdev/gh-budget/core.json"
    budget.parent.mkdir(parents=True)
    for payload, remaining in [("[]", 1), (None, 0)]:
        budget.write_text(json.dumps({"remaining": remaining}))
        prompt(tmp_path, "s1", monkeypatch)
        if payload is not None:
            monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        assert alpha.main(["chase-context"], "tbhbdev alpha") == 0
        assert capsys.readouterr() == ("", "")
        assert not (tmp_path / ".local/state/tbhb").exists()


def test_empty_refresh_stays_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def scanner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "no stale pull requests\n")

    budget = tmp_path / ".local/state/tbhbdev/gh-budget/core.json"
    budget.parent.mkdir(parents=True)
    budget.write_text('{"remaining": 1}')
    monkeypatch.setenv("TBHB_STALE_PRS", str(tmp_path / "scanner"))
    monkeypatch.setattr(chase_context.subprocess, "run", scanner)
    prompt(tmp_path, "s1", monkeypatch)
    assert alpha.main(["chase-context"], "tbhbdev alpha") == 0
    assert capsys.readouterr() == ("", "")
    assert (tmp_path / ".local/state/tbhb/chase-context.cache").read_text() == ""


def test_unconfigured_refresh_skips_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TBHB_STALE_PRS", raising=False)
    assert chase_context.refresh() == ""
    monkeypatch.setenv("TBHB_STALE_PRS", "")
    assert chase_context.refresh() == ""
