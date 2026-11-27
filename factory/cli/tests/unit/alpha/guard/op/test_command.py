# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise the 1Password hook with shell calls and proposed changes."""

import json
from typing import TYPE_CHECKING, Never

import pytest

from tbhbdev import userconfig
from tbhbdev.alpha.guard.op import command as guard

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path


ACCOUNT = "example.1password.com"


def configured(table: str, key: str) -> str:
    assert (table, key) == ("op", "account")
    return ACCOUNT


@pytest.fixture(autouse=True)
def account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userconfig, "string", configured)


FILES = [
    ("x.sh", "op read x\n"),
    ("x.bash.j2", "op read x\n"),
    ("x", "#!/usr/bin/env zsh\nop read x\n"),
    ("x.md", "```bash\nop read x\n```\n"),
    ("x.md", "~~~console\n$ op read x\nignored op read y\n# op read z\n~~~\n"),
    ("x.py", '["/opt/homebrew/bin/op", "read", ref]\n'),
    ("x.py", 'subprocess.run("op read x", shell=True)\n'),
    ("x.py", 'os.system("op read x")\n'),
    ("x.py", 'os.popen("op read x")\n'),
    ("x.yml", "jobs:\n  build:\n    steps:\n      - run: op read x\n"),
    ("x.yaml", "- shell: op item get x\n"),
    ("x.yml", "argv: [op, read, x]\n"),
]


@pytest.mark.parametrize(("name", "content"), FILES)
def test_write_new_old_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: str,
) -> None:
    path = tmp_path / name
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": content},
    }
    monkeypatch.setattr(guard, "read_hook_stdin", lambda: payload)
    assert guard.main() == 0
    assert "permissionDecision" in capsys.readouterr().out
    path.write_text(content)
    payload["tool_input"]["content"] = content + "\n"
    assert guard.main() == 0
    assert not capsys.readouterr().out
    payload["tool_input"]["content"] = content.replace("read", "delete").replace(
        "get", "delete"
    )
    assert guard.main() == 0
    assert "permissionDecision" in capsys.readouterr().out


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: Mapping[str, object],
) -> str:
    payload = dict(payload)
    call = payload.get("toolCall")
    if isinstance(call, dict):
        payload["toolCall"] = {"name": "run_command", **call}
    else:
        payload.setdefault("tool_name", "Bash")
    monkeypatch.setattr(guard, "read_hook_stdin", lambda: payload)
    assert guard.main() == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("flag", ["", " --account example.1password.com"])
def test_patch_and_agy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    patch = f"*** Begin Patch\n*** Add File: new.sh\n+op read x{flag}\n*** End Patch"
    payload = {
        "cwd": str(tmp_path),
        "tool_name": "apply_patch",
        "tool_input": {"command": patch},
    }
    assert bool(invoke(monkeypatch, capsys, payload)) == (not flag)
    payload = {
        "toolCall": {
            "name": "write_to_file",
            "args": {
                "TargetFile": str(tmp_path / "x.sh"),
                "CodeContent": f"op read x{flag}",
            },
        }
    }
    assert bool(invoke(monkeypatch, capsys, payload)) == (not flag)


@pytest.mark.parametrize(
    "tool", ["Edit", "MultiEdit", "replace_file_content", "multi_replace_file_content"]
)
def test_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str,
) -> None:
    path = tmp_path / "x.sh"
    path.write_text("echo x\n")
    edit = {"old_string": "echo", "new_string": "op"}
    inputs = {"file_path": str(path), **edit}
    if tool == "MultiEdit":
        inputs = {"file_path": str(path), "edits": [edit]}
    if tool in {"replace_file_content", "multi_replace_file_content"}:
        inputs = {
            "TargetFile": str(path),
            "ReplacementChunks": [
                {"TargetContent": "echo", "ReplacementContent": "op"}
            ],
        }
        payload = {"toolCall": {"name": tool, "args": json.dumps(inputs)}}
    else:
        payload = {"tool_name": tool, "tool_input": inputs}
    assert invoke(monkeypatch, capsys, payload)


def test_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(*_: object) -> Never:
        raise RuntimeError

    monkeypatch.setattr(guard, "read_hook_stdin", crash)
    assert guard.main() == 0
    assert not capsys.readouterr().out
    monkeypatch.setattr(
        guard,
        "read_hook_stdin",
        lambda: {"tool_name": "Bash", "tool_input": {"command": "op read x"}},
    )
    monkeypatch.setattr(userconfig, "string", crash)
    assert guard.main() == 0
    assert "Set [op] account" in capsys.readouterr().out
    monkeypatch.setattr(userconfig, "string", configured)

    def partial(_: str) -> Iterator[list[str]]:
        yield ["op", "read", "x", "--account", ACCOUNT]
        raise RuntimeError

    monkeypatch.setattr(guard, "shell_calls", partial)
    assert guard.main() == 0
    out = capsys.readouterr().out
    assert "add --account example.1password.com" in out
    assert "`op read x --account example.1password.com`" in out


@pytest.mark.parametrize("inputs", [None, {}])
def test_other_payloads(
    inputs: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not invoke(monkeypatch, capsys, {"tool_input": inputs})


def test_denial_contracts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = (
        "`op read x` is blocked. Use the configured account example.1password.com: "
        "add --account example.1password.com."
    )
    assert json.loads(
        invoke(monkeypatch, capsys, {"tool_input": {"command": "op read x"}})
    ) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }
    assert json.loads(
        invoke(
            monkeypatch, capsys, {"toolCall": {"args": {"CommandLine": "op read x"}}}
        )
    ) == {
        "decision": "deny",
        "reason": message,
    }
    assert not invoke(
        monkeypatch,
        capsys,
        {"tool_input": {"command": f"op read x --account {ACCOUNT}"}},
    )


def test_missing_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_table: str, _key: str) -> str:
        raise userconfig.ConfigError

    monkeypatch.setattr(userconfig, "string", missing)
    assert "Set [op] account in the user config." in invoke(
        monkeypatch, capsys, {"tool_input": {"command": "op read x"}}
    )
    assert not invoke(monkeypatch, capsys, {"tool_input": {"command": "echo op"}})


def test_edit_set_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "script.sh"
    path.write_text("op read x\n")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": "op read x\nop read x\n"},
    }
    assert not invoke(monkeypatch, capsys, payload)


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
@pytest.mark.parametrize("flag", ["", " --account example.1password.com"])
def test_agy_cli_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    flag: str,
) -> None:
    path = tmp_path / "cli.sh"
    path.write_text("echo x\n")
    args = (
        {"Path": str(path), "Content": f"op read x{flag}"}
        if tool == "write_file"
        else {
            "Path": str(path),
            "OldText": "echo x",
            "NewText": f"op read x{flag}",
            "ReplaceAll": True,
        }
    )
    assert bool(
        invoke(monkeypatch, capsys, {"toolCall": {"name": tool, "args": args}})
    ) == (not flag)


def test_main_retains_first_patch_offender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = (
        "*** Begin Patch\n*** Add File: x.sh\n+op read a\n"
        "*** Add File: y.sh\n+echo safe\n*** End Patch"
    )
    out = invoke(
        monkeypatch,
        capsys,
        {
            "cwd": str(tmp_path),
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
        },
    )
    assert "op read a" in out
    patch = "*** Begin Patch\n*** Add File: x.sh\n+op read a\n+op read z\n*** End Patch"
    out = invoke(
        monkeypatch,
        capsys,
        {
            "cwd": str(tmp_path),
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
        },
    )
    assert "`op read a`" in out


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("x.py", '["op", "read", ref, "--account", ACCOUNT]'),
        ("x.yaml", "argv: [op, read, x, --account, anything]"),
    ],
)
def test_argv_needs_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: str,
) -> None:
    def missing(_table: str, _key: str) -> str:
        raise userconfig.ConfigError

    monkeypatch.setattr(userconfig, "string", missing)
    assert "Set [op] account" in invoke(
        monkeypatch,
        capsys,
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / name), "content": content},
        },
    )


@pytest.mark.parametrize("suffix", ["sh", "md"])
def test_patch_context_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
) -> None:
    path = tmp_path / f"context.{suffix}"
    path.write_text(" op item get old\necho old\n")
    patch = (
        f"*** Begin Patch\n*** Update File: {path.name}\n@@\n"
        "  op item get old\n-echo old\n+echo new\n*** End Patch"
    )
    assert not invoke(
        monkeypatch,
        capsys,
        {
            "tool_name": "apply_patch",
            "cwd": str(tmp_path),
            "tool_input": {"command": patch},
        },
    )


@pytest.mark.parametrize(
    "tool", ["Bash", "shell", "run_command", "apply_patch", "Write", "unknown"]
)
def test_shell_tool_selection(
    tool: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = (
        {"toolCall": {"name": tool, "args": {"CommandLine": "op read x"}}}
        if tool == "run_command"
        else {"tool_name": tool, "tool_input": {"command": "op read x"}}
    )
    assert bool(invoke(monkeypatch, capsys, payload)) == (
        tool in {"Bash", "shell", "run_command"}
    )
