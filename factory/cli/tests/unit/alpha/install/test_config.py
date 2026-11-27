"""Tests for install config."""

import json
import os
from typing import TYPE_CHECKING, cast

from unit.alpha.install.expectations import THEIRS, marked

from tbhbdev.alpha.install import config as harness_config
from tbhbdev.alpha.install import files, hooks

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tbhbdev.alpha.install.model import Json


def test_install_config_writes_then_is_up_to_date(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = isolated_home / ".claude" / "settings.json"
    assert harness_config.install_config("claude", {}, dry_run=False) == 0
    assert capsys.readouterr().out == f"claude: wrote {path}\n"
    assert json.loads(path.read_text()) == {
        "hooks": hooks.claude_hooks(),
        "env": {"SSH_AUTH_SOCK": ""},
    }
    assert harness_config.install_config("claude", {}, dry_run=False) == 0
    assert capsys.readouterr().out == f"claude: {path} is up to date\n"
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


def installed_claude_hooks(isolated_home: Path) -> dict[str, list[Json]]:
    path = isolated_home / ".claude" / "settings.json"
    return cast("dict[str, list[Json]]", json.loads(path.read_text())["hooks"])


def test_a_fresh_claude_install_has_only_the_run_stop_hook_and_no_send_message(
    isolated_home: Path,
) -> None:
    assert harness_config.install_config("claude", {}, dry_run=False) == 0
    events = installed_claude_hooks(isolated_home)
    assert events["Stop"] == hooks.stop_hooks("claude")["Stop"]
    guard = marked("state-guard")
    matchers = [
        g.get("matcher")
        for g in events["PreToolUse"]
        for h in cast("list[Json]", g["hooks"])
        if h["command"] == guard
    ]
    assert matchers == ["Bash|Write|Edit"]
    assert "SendMessage" not in json.dumps(events)


def test_a_claude_install_strips_the_old_send_message_and_stop_guards(
    isolated_home: Path,
) -> None:
    path = isolated_home / ".claude" / "settings.json"
    path.parent.mkdir()
    guard = {"type": "command", "command": marked("state-guard"), "timeout": 10}
    old = {
        "hooks": {
            "Stop": [{"hooks": [THEIRS]}, {"hooks": [guard]}],
            "PreToolUse": [
                {"matcher": "Bash|SendMessage|Write|Edit", "hooks": [guard]},
                {"matcher": "SendMessage", "hooks": [THEIRS]},
            ],
        }
    }
    path.write_text(json.dumps(old))
    assert harness_config.install_config("claude", {}, dry_run=False) == 0
    events = installed_claude_hooks(isolated_home)
    assert events["Stop"] == [
        {"hooks": [THEIRS]},
        *hooks.stop_hooks("claude")["Stop"],
    ]
    pre = events["PreToolUse"]
    assert {"matcher": "SendMessage", "hooks": [THEIRS]} in pre
    matchers = [g.get("matcher") for g in pre]
    assert "Bash|SendMessage|Write|Edit" not in matchers
    assert "Bash|Write|Edit" in matchers


def test_install_config_keeps_the_users_settings_and_backs_up(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(files, "stamp", lambda: "T")
    path = isolated_home / ".claude" / "settings.json"
    path.parent.mkdir()
    old = {"model": "opus", "hooks": {"Stop": [{"hooks": [THEIRS]}]}}
    path.write_text(json.dumps(old))
    assert harness_config.install_config("claude", {}, dry_run=False) == 0
    backup = path.with_name("settings.json.tbhbdev-backup.T")
    assert capsys.readouterr().out == (
        f"claude: wrote {path}; the old one is {backup}\n"
    )
    assert json.loads(backup.read_text()) == old
    assert json.loads(path.read_text()) == {
        "model": "opus",
        "hooks": {
            **hooks.claude_hooks(),
            "Stop": [{"hooks": [THEIRS]}, *hooks.stop_hooks("claude")["Stop"]],
        },
        "env": {"SSH_AUTH_SOCK": ""},
    }


def test_install_config_codex_trusts_its_hooks(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    codex = isolated_home / "codex-home"
    env = {"CODEX_HOME": str(codex)}
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    assert capsys.readouterr().out == (
        f"codex: wrote {codex / 'hooks.json'}\n"
        f"codex: trusted 12 hooks in {codex / 'config.toml'}\n"
    )


def test_install_config_dry_run_prints_the_diff_and_writes_nothing(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = isolated_home / ".gemini" / "config" / "hooks.json"
    assert harness_config.install_config("antigravity", {}, dry_run=True) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"--- {path}\n+++ {path}\n@@ -0,0 +1,")
    assert '+  "tbhbdev-alpha-identity": {\n' in out
    assert not path.exists()
    path.parent.mkdir(parents=True)
    path.write_text('{"mine": {}}\n')
    assert harness_config.install_config("antigravity", {}, dry_run=True) == 0
    out = capsys.readouterr().out
    assert '\n-{"mine": {}}\n+{\n+  "mine": {},\n' in out
    assert out.endswith("\n+}\n")
    assert not out.endswith("\n\n")
    assert path.read_text() == '{"mine": {}}\n'


def test_install_config_reports_a_config_it_cannot_merge(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = isolated_home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"hooks": 1}')
    assert harness_config.install_config("claude", {}, dry_run=False) == 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha install: claude: cannot merge into {path}: "
        "its hooks key is not an object\n"
    )
    assert path.read_text() == '{"hooks": 1}'


def test_install_config_reports_a_config_it_cannot_read(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = isolated_home / ".claude" / "settings.json"
    path.mkdir(parents=True)
    assert harness_config.install_config("claude", {}, dry_run=False) == 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha install: claude: cannot merge into {path}: it cannot be "
        f"read ([Errno 21] Is a directory: {str(path)!r})\n"
    )


def test_install_config_reports_a_config_it_cannot_write(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = isolated_home / ".codex"
    config.mkdir()
    config.chmod(0o500)
    try:
        assert harness_config.install_config("codex", {}, dry_run=False) == 1
        tmp = config / f".hooks.json.{os.getpid()}.tmp"
        assert capsys.readouterr() == (
            "",
            (
                f"tbhbdev alpha install: codex: cannot write {config / 'hooks.json'}: "
                f"[Errno 13] Permission denied: {str(tmp)!r}\n"
            ),
        )
        assert list(config.iterdir()) == []
    finally:
        config.chmod(0o700)


def test_install_config_fills_an_empty_config(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(files, "stamp", lambda: "T")
    path = isolated_home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text("\n")
    assert harness_config.install_config("claude", {}, dry_run=False) == 0
    backup = path.with_name("settings.json.tbhbdev-backup.T")
    assert capsys.readouterr().out == (
        f"claude: wrote {path}; the old one is {backup}\n"
    )
    assert backup.read_text() == "\n"
    assert json.loads(path.read_text()) == {
        "hooks": hooks.claude_hooks(),
        "env": {"SSH_AUTH_SOCK": ""},
    }


def test_install_config_other_harnesses_trust_nothing(isolated_home: Path) -> None:
    for harness in ("claude", "antigravity"):
        assert harness_config.install_config(harness, {}, dry_run=False) == 0
    assert not (isolated_home / ".codex").exists()
