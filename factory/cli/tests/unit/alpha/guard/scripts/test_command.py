# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise the script hook's input and output contracts."""

import io
import json
import sys
from typing import TYPE_CHECKING

import pytest
from unit.alpha.guard.scripts.fixtures import bash, outside, patch, tool

from tbhbdev.alpha.guard.scripts.paths import TMP_ROOT

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

from tbhbdev.alpha.guard.scripts import command as guard

type Json = dict[str, object]


def test_a_missing_home_fails_open(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HOME", raising=False)
    payload = json.dumps(tool(home, "Write", file_path=f"{TMP_ROOT}/a.sh"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def run_hook(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    payload: object,
    *,
    tmpdir: str | None = None,
) -> int:
    monkeypatch.setenv("HOME", str(home))
    if tmpdir is None:
        monkeypatch.delenv("TMPDIR", raising=False)
    else:
        monkeypatch.setenv("TMPDIR", tmpdir)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return guard.main()


@pytest.mark.parametrize("agy", [False, True])
def test_hook_contract(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    agy: bool,
) -> None:
    command = "echo x > ~/.cache/a.sh"
    payload = (
        {"toolCall": {"name": "run_command", "args": {"CommandLine": command}}}
        if agy
        else bash(home, command)
    )
    assert run_hook(monkeypatch, home, payload) == 0
    output = json.loads(capsys.readouterr().out)
    reason = outside(home / ".cache" / "a.sh")
    if agy:
        assert output == {"decision": "deny", "reason": reason}
    else:
        assert list(output) == ["hookSpecificOutput"]
        inner = output["hookSpecificOutput"]
        assert inner["hookEventName"] == "PreToolUse"
        assert inner["permissionDecision"] == "deny"
        assert inner["permissionDecisionReason"] == reason


def test_hook_contract_for_an_agy_write_file(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"toolCall": {"name": "write_file", "args": {"Path": "~/.cache/a.sh"}}}
    assert run_hook(monkeypatch, home, payload) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"decision": "deny", "reason": outside(home / ".cache" / "a.sh")}


def test_hook_contract_for_a_codex_patch(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = tool(
        home, "apply_patch", command=patch("*** Add File: ~/.cache/a.sh", "+x")
    )
    assert run_hook(monkeypatch, home, payload) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_reads_tmpdir_from_the_environment(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = tool(home, "Write", file_path=f"{TMP_ROOT}/run-1/a.sh")
    assert run_hook(monkeypatch, home, payload, tmpdir=f"{TMP_ROOT}/run-1") == 0
    assert capsys.readouterr() == ("", "")
    assert run_hook(monkeypatch, home, payload) == 0
    assert "deny" in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    ["invalid", "[]", "{}", '{"tool_input":{"command":"true"}}', ""],
)
def test_hook_allows_unreadable_or_allowed_input(
    payload: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    status = guard.main()
    assert (status, capsys.readouterr().out) == (0, "")


def test_hook_crashes_fail_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(_data: Json, _env: Mapping[str, str]) -> None:
        raise RuntimeError

    monkeypatch.setattr(guard, "first_refusal", crash)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_input": {}}'))
    status = guard.main()
    assert (status, capsys.readouterr().out) == (0, "")


def test_deep_nesting_fails_open(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command = "eval " * 1000 + "echo > ~/.cache/a.sh"
    assert run_hook(monkeypatch, home, bash(home, command)) == 0
    assert capsys.readouterr().out == ""
