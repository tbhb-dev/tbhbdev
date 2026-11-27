# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for the SSH agent guard."""

import codecs
import io
import json
import socket
import sys
from pathlib import Path
from typing import override

import pytest

import tbhbdev.alpha.guard_ssh_agent as guard

S = ".ssh"


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("ssh-add -l key", guard.REASON_ADD),
        ("/usr/bin/ssh-add", guard.REASON_ADD),
        ("true && ssh-add", guard.REASON_ADD),
        ("\\ssh-add", guard.REASON_ADD),
        ("true |& ssh-add", guard.REASON_ADD),
        ("echo ok & ssh-add", guard.REASON_ADD),
        ("echo a\nssh-add", guard.REASON_ADD),
        ("env FOO=1 ssh-add", guard.REASON_ADD),
        ("/usr/bin/env ssh-add", guard.REASON_ADD),
        ("/usr/local/opt/coreutils/libexec/gnubin/env ssh-add", guard.REASON_ADD),
        ("/usr/bin/../bin/env ssh-add", guard.REASON_ADD),
        ("/usr/bin/../bin/nohup ssh-add", guard.REASON_ADD),
        ("./time ssh-add", guard.REASON_ADD),
        ("tools/env ssh-add", guard.REASON_ADD),
        ("/home/u/project/command ssh-add", guard.REASON_ADD),
        ("nohup ssh-add", guard.REASON_ADD),
        ("exec ssh-add", guard.REASON_ADD),
        ("time ssh-add", guard.REASON_ADD),
        ("ssh-add; sudo -nl", guard.REASON_ADD),
        ("FOO=1 command ssh-add", guard.REASON_ADD),
        ("ssh-add -l; echo $'done'; ssh-add", guard.REASON_ADD),
        ('"ssh-\\\nadd"', guard.REASON_ADD),
        ("{ ssh-add; }", guard.REASON_ADD),
        ("[[ -n $x ]] && ssh-add", guard.REASON_ADD),
    ],
)
def test_denied(command: str, reason: str) -> None:
    assert guard.verdict(command) == reason


@pytest.mark.parametrize(
    "command",
    [
        "ssh-add -l",
        'echo "Build (debug)"',
        "/usr/bin/env ssh-add -l",
        f"ls ~/{S}/sessions",
        f"ls ~/{S}x",
        f"git commit -m {Path.home()}/{S}",
        f"cat ./{S}/config",
        f"echo ~/{S}/config",
        f"git commit -m 'read config=~/{S}/config'",
        "SSH_AUTH_SOCK=/s git status",
        "env FOO=1",
        f"cat <<EOF\nLocation: ~/{S}/config\nssh-add -l\n~/{S}/id\nEOF",
        "echo '|' ssh-add",
        "command -v ssh-add",
        f"grep -F '~/{S}/config' reference.txt",
        "git status  # ssh-add",
        "cat <<'X'\nssh-add\nX\nls",
        "cat <<'END-DOC'\nssh-add\nEND-DOC\nls",
        "cat <<-EOF\n\tssh-add\nEOF",
        f'git commit -m "$HOME/{S}/config"',
        f'grep -F "$HOME/{S}/config" notes.txt',
        f'cat <<< "$HOME/{S}/config"',
        f"grep -F '\\$HOME/{S}' notes.txt",
        "cat < ./ssh-add",
        "cat < 'ssh-add'",
        "cat <<< 'ssh-add'",
        "case $x in\n ssh-add) true;;\nesac",
        "command -pv ssh-add",
        "sudo -l ssh-add",
        f"git commit -m `echo ~/{S}/x`",
        "(ssh-add)",
        "ssh-add <<EOF\nunterminated",
        "ls # <<EOF\nssh-add",
        "cat <<EOF\nx\nEOF\nssh-add",
        "cat <<EOF\n  EOF\nssh-add -l\nEOF",
        "cat <<'-EOF'\n-EOF\nssh-add -l",
        'git commit -m "$(printf \'%s\' "docs; ssh-add -l; finished")"',
        "echo foo &> ./ssh-add",
        "(case $x in ssh-add) true;; esac)",
        f"git commit -m ~/{S}/config",
        f"grep -F ~/{S}/config x",
        f"cat ~/{S}/config",
        "echo foo &> out ssh-add",
        "echo &>>log ssh-add",
        "printf %s >|file ssh-add",
        "ssh-add 2>&1",
        "< notes ssh-add",
        "sudo ssh-add",
        "sudo -nl ssh-add",
        "sudo -ll ssh-add",
        "sudo -lUx ssh-add",
        "sudo -e ./ssh-add",
        "doas ssh-add",
        "env FOO=1 sudo ssh-add",
        "env -- FOO=1 ssh-add",
        "env --unset FOO ssh-add -l",
        "env -S 'printf %s' ssh-add",
        "env --chdir=./ssh-add pwd",
        "/home/u/project/nohup ssh-add",
        "printf '%s\\n' $'it\\'s text; ssh-add; # end'",
        "/usr/bin/time -o./ssh-add /usr/bin/true",
        "env -C./ssh-add pwd",
        "env --split-string='printf %s ./ssh-add'",
        "command -- ssh-add",
        "printf '%s\\n' ${STATUS:-ready;ssh-add;done}",
        "if true; then ssh-add; fi",
        "[[ -z $t || ssh-add == $t ]] && echo matched",
        "[[ $t =~ git|ssh-add ]]",
        "ssh-add; echo (x)",
        "ssh-add; case x in",
    ],
)
def test_allowed(command: str) -> None:
    assert guard.verdict(command) is None


def test_quoting_and_continuations() -> None:
    assert guard.verdict('echo "a \\"b\\" \\$x"') is None
    assert guard.verdict("ls \\\n -l") is None


def test_ansi_c_quote_keeps_the_read_only_command_in_the_parse() -> None:
    command = "ssh-add -l; echo $'done'"
    assert guard.segments(command) == [["ssh-add", "-l"], ["echo", "done"]]
    assert guard.verdict(command) is None


def test_single_brackets_are_preserved_as_shell_words() -> None:
    assert guard.segments("[ -n a[b] ]") == [["[", "-n", "a[b]", "]"]]


@pytest.mark.parametrize("command", ["echo 'unbalanced", 'echo "Build (debug)'])
def test_an_unterminated_quote_is_reported(command: str) -> None:
    assert guard.verdict(command) == guard.REASON_QUOTE


def test_a_bare_name_counts_when_the_file_is_in_the_ssh_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / S
    ssh.mkdir()
    (ssh / "id_x").write_text("k")
    assert guard.path_reason({"tool_input": {"path": "id_x"}}, str(ssh)) is not None
    assert guard.path_reason({"tool_input": {"path": "main"}}, str(ssh)) is None
    assert guard.path_reason({"tool_input": {"path": "-s"}}, str(ssh)) is None
    (ssh / "-s").touch()
    reason = guard.path_reason({"tool_input": {"path": "-s"}}, str(ssh))
    assert reason == guard.REASON_DIR


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires Unix sockets")
def test_a_socket_outside_ssh_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind("agent.sock")
        reason = guard.path_reason(
            {"tool_input": {"path": "agent.sock"}}, str(tmp_path)
        )
        assert reason


@pytest.mark.parametrize(
    ("payload", "antigravity"),
    [
        (b'{"tool_input": {"command": "echo \xff"}}', False),
        (b'{"toolCall": {"args": {"CommandLine": "echo \xff"}}}', True),
    ],
)
def test_non_utf8_input_is_reported(
    payload: bytes,
    *,
    antigravity: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        assert guard.main() == 0
    decision = json.loads(capsys.readouterr().out)
    if antigravity:
        assert decision["decision"] == "deny"
    else:
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_non_utf8_text_stream_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = codecs.getreader("utf-8")(io.BytesIO(b"\xff"))
    monkeypatch.setattr(sys, "stdin", stdin)
    assert guard.read_payload() == ({}, guard.REASON_ENCODING)


def test_invalid_json_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))
    assert guard.denial() is None


def test_the_payload_cwd_resolves_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    payload = {"cwd": str(tmp_path), "tool_input": {"file_path": f"{S}/config"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    assert "deny" in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload | {"cwd": "/"})))
    assert guard.main() == 0
    assert capsys.readouterr().out == ""


class ClosedPipe(io.StringIO):
    @override
    def flush(self) -> None:
        raise BrokenPipeError


def test_a_closed_output_pipe_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"tool_input": {"command": "ssh-add"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    assert guard.main() == 0


@pytest.mark.parametrize("agy", [False, True])
def test_hook_contract(
    *,
    agy: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if agy:
        stdin = json.dumps({"toolCall": {"args": {"CommandLine": "ssh-add"}}})
    else:
        stdin = json.dumps({"tool_input": {"command": "ssh-add"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert guard.main() == 0
    out = json.loads(capsys.readouterr().out)
    if agy:
        assert out == {"decision": "deny", "reason": guard.REASON_ADD}
    else:
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    ("tool_input", "denied"),
    [
        ({"file_path": f"{Path.home()}/{S}/id_x"}, True),
        ({"path": f"~/{S}/config"}, True),
        ({"file_path": f"{Path.home()}/{S}/known hosts"}, True),
        ({"file_path": f"{Path.home()}/{S}/sessions/k"}, False),
        ({"file_path": "/etc/hosts"}, False),
        ({"file_path": 3}, False),
        ({"file_path": ""}, False),
    ],
)
def test_a_tool_path_is_checked(
    *,
    tool_input: dict[str, object],
    denied: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"tool_input": tool_input}))
    )
    assert guard.main() == 0
    assert bool(capsys.readouterr().out) is denied


def test_sessions_matches_by_file_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case-insensitive volume names sessions as SESSIONS: samefile says so."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / S / "sessions").mkdir(parents=True)

    def same(_self: Path, _other: object) -> bool:
        return True

    monkeypatch.setattr(guard.Path, "samefile", same)
    payload: dict[str, object] = {"tool_input": {"file_path": f"~/{S}/SESSIONS/record"}}
    assert guard.path_reason(payload) is None


def test_a_symlinked_sessions_directory_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / S
    (ssh / "store").mkdir(parents=True)
    (ssh / "sessions").symlink_to("store")
    (ssh / "store" / "record").write_text("")
    payload: dict[str, object] = {"tool_input": {"file_path": f"~/{S}/sessions/record"}}
    assert guard.path_reason(payload) is None


def test_a_payload_without_tool_input_is_allowed() -> None:
    assert guard.path_reason({"tool_input": "x"}) is None


def test_a_crash_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_text: str) -> str:
        raise RuntimeError

    monkeypatch.setattr(guard, "verdict", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert guard.main() == 0
    assert capsys.readouterr().out == ""
