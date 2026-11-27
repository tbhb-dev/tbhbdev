"""Tests for the `tbhbdev` command's dispatch to its tools."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev import cli

if TYPE_CHECKING:
    from pathlib import Path

USAGE = cli.usage(cli.TOOLS)


def test_no_arguments_prints_usage_to_stderr_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == USAGE


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_to_stdout(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([flag]) == 0
    captured = capsys.readouterr()
    assert captured.out == USAGE
    assert captured.err == ""


def test_usage_lists_every_tool() -> None:
    for name in cli.TOOLS:
        assert f"  {name}\n" in USAGE


@pytest.mark.parametrize("name", ["nope", "host"])
def test_unknown_tool_fails_with_usage(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([name, "x"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"tbhbdev: unknown tool {name!r}\n{USAGE}"


def test_tool_gets_the_rest_of_argv_and_its_program_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str]] = []

    def tool(argv: list[str], prog: str) -> int:
        calls.append((argv, prog))
        return 7

    monkeypatch.setitem(cli.TOOLS, "fake", tool)
    assert cli.main(["fake", "a", "--b"]) == 7
    assert calls == [(["a", "--b"], "tbhbdev fake")]


def test_main_reads_sys_argv_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["tbhbdev", "--help"])
    assert cli.main() == 0


def test_module_entry_point_runs_the_command(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == USAGE
