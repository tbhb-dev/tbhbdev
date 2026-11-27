# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity ancestry tests."""

import os
import subprocess

import pytest
from integration.alpha.identity.identity_test_support import FakeRun, returning

from tbhbdev.alpha.identity import ancestry


def test_run_ps_parses_parent_and_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str], **kwargs: object) -> FakeRun:
        calls.append(argv)
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        return FakeRun(0, "  42 /bin/zsh -c  eval x \n")

    monkeypatch.setattr(subprocess, "run", fake)
    assert ancestry.run_ps("7") == ("42", "/bin/zsh -c  eval x")
    assert calls == [["ps", "-ww", "-o", "ppid=,command=", "-p", "7"]]


def test_run_ps_none_when_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", returning(FakeRun(1, "1 x\n")))
    assert ancestry.run_ps("7") is None
    monkeypatch.setattr(subprocess, "run", returning(FakeRun(0, "\n")))
    assert ancestry.run_ps("7") is None
    monkeypatch.setattr(subprocess, "run", returning(FakeRun(1, "")))
    assert ancestry.run_ps("7") is None


def test_run_ps_none_when_ps_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(argv: list[str], **_: object) -> FakeRun:
        raise PermissionError(argv[0])

    monkeypatch.setattr(subprocess, "run", denied)
    assert ancestry.run_ps("7") is None


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


def test_ps_row_on_real_processes() -> None:
    row = ancestry.ps_row(str(os.getpid()))
    assert row is not None
    assert row[0].isdigit()
    # pytest-xdist retitles its workers ("[pytest-xdist running] ..."), so
    # check the command against what ps itself reports rather than "python".
    assert row[1] == ps_command(os.getpid())
    assert ancestry.ps_row("999999999") is None


def fake_ps(monkeypatch: pytest.MonkeyPatch, rows: dict[str, tuple[str, str]]) -> None:
    monkeypatch.setattr(ancestry, "ps_row", rows.get)


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
    assert ancestry.ancestry(10) == [
        "/bin/zsh -c eval a",
        "node tool",
        "/usr/local/bin/claude --flag",
    ]
    assert ancestry.ancestry(30) == ["/usr/local/bin/claude --flag"]


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
    assert ancestry.ancestry(10) == ["a", "b"]
    assert ancestry.ancestry(30) == ["c"]
    assert ancestry.ancestry(40) == ["d"]
    assert ancestry.ancestry(70) == ["g"]
    assert ancestry.ancestry(50, limit=3) == ["e", "f", "e"]
    assert ancestry.ancestry(50, limit=0) == []
    assert len(ancestry.ancestry(50)) == ancestry.ANCESTRY_LIMIT


def test_ancestry_defaults_to_this_process() -> None:
    # Compared with ps, not a "python" substring: xdist workers retitle themselves.
    chain = ancestry.ancestry()
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
    assert ancestry.ancestry(10) == ["/bin/claude --a b"]
    assert ancestry.ancestry(20) == ["claude"]
