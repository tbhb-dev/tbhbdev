# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity claude ancestry."""

from __future__ import annotations

import os
import subprocess

import pytest

from tbhbdev.alpha.identity.claude import ancestry as claude_ancestry


def ps_command(pid: int) -> str:
    """What ps reports for a process; skips the test where a sandbox denies ps."""
    try:
        done = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
        )
    except PermissionError:
        pytest.skip("this sandbox denies ps")
    return done.stdout.strip()


def test_ancestry_reads_real_processes() -> None:
    rows = claude_ancestry.ancestry(os.getpid(), limit=1)
    # pytest-xdist retitles its workers ("[pytest-xdist running] ..."), so
    # check the command against what ps itself reports rather than "python".
    assert rows == [ps_command(os.getpid())]
    assert claude_ancestry.ancestry(999999999) == []


def fake_ps(monkeypatch: pytest.MonkeyPatch, rows: dict[str, tuple[str, str]]) -> None:
    monkeypatch.setattr(claude_ancestry, "process_row", rows.get)


def test_ancestry_walks_up_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ps(
        monkeypatch,
        {
            "10": ("20", "/bin/zsh -c eval a"),
            "20": ("30", "node tool"),
            "30": ("40", "/usr/local/bin/claude --flag"),
            "40": ("1", "login"),
        },
    )
    assert claude_ancestry.ancestry(10) == [
        "/bin/zsh -c eval a",
        "node tool",
        "/usr/local/bin/claude --flag",
    ]
    assert claude_ancestry.ancestry(30) == ["/usr/local/bin/claude --flag"]


def test_ancestry_stops_at_init_and_gaps_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ps(
        monkeypatch,
        {
            "10": ("20", "a"),
            "20": ("1", "b"),
            "30": ("0", "c"),
            "40": ("", "d"),
            "50": ("60", "e"),
            "60": ("50", "f"),
            "70": ("999", "g"),
            "": ("999", "after-empty"),
            "0": ("999", "after-zero"),
            "1": ("999", "after-one"),
        },
    )
    assert claude_ancestry.ancestry(10) == ["a", "b"]
    assert claude_ancestry.ancestry(30) == ["c"]
    assert claude_ancestry.ancestry(40) == ["d"]
    assert claude_ancestry.ancestry(70) == ["g"]
    assert claude_ancestry.ancestry(50, limit=3) == ["e", "f", "e"]
    assert claude_ancestry.ancestry(50, limit=0) == []
    assert len(claude_ancestry.ancestry(50)) == claude_ancestry.ANCESTRY_LIMIT


def test_ancestry_defaults_to_this_process() -> None:
    # Compared with ps, not a "python" substring: xdist workers retitle themselves.
    chain = claude_ancestry.ancestry()
    assert chain[:2] == [ps_command(os.getpid()), ps_command(os.getppid())]


def test_ancestry_recognizes_claude_by_its_first_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ps(
        monkeypatch,
        {
            "10": ("20", "/bin/claude --a b"),
            "20": ("30", "claude"),
            "30": ("1", "login"),
        },
    )
    assert claude_ancestry.ancestry(10) == ["/bin/claude --a b"]
    assert claude_ancestry.ancestry(20) == ["claude"]
