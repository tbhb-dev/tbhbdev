"""Tests for install command."""

import hashlib
import json
import shutil
import tomllib
from typing import TYPE_CHECKING

import pytest
from unit.alpha.install.expectations import (
    FOREIGN_CONFIG,
    marked,
    our_trust,
    shim_path,
    trust_hash,
    trusted_in,
)

from tbhbdev.alpha import gh_shim
from tbhbdev.alpha.install import command, hooks

if TYPE_CHECKING:
    from pathlib import Path

    from unit.alpha.install.conftest import World


def test_main_installs_the_ssh_shim_beside_gh(world: World) -> None:
    assert command.main([], "p") == 0
    assert sorted(p.name for p in gh_shim.bin_dir().iterdir()) == ["gh", "ssh"]
    del world


def test_main_fails_when_only_the_statusline_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def step(*_args: object, **_kwargs: object) -> int:
        return 0

    def statusline(_env: object, *, dry_run: object) -> int:
        assert dry_run is False
        return 1

    for name in (
        "install_git_hooks",
        "install_gh_shim",
        "install_config",
    ):
        monkeypatch.setattr(command, name, step)
    monkeypatch.setattr(command, "install_statusline", statusline)
    assert command.main([], "p") == 1


def test_main_installs_the_git_hooks_and_every_harness(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command.main([], "tbhbdev alpha install") == 0
    out = capsys.readouterr().out
    for path in (
        world.home / ".claude" / "settings.json",
        world.home / ".codex" / "hooks.json",
        world.home / ".gemini" / "config" / "hooks.json",
    ):
        assert f"wrote {path}\n" in out
        assert path.is_file()
    assert (world.hooks / "commit-msg").is_file()


def test_main_honors_the_harness_config_variables(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert command.main([], "p") == 0
    assert (tmp_path / "claude" / "settings.json").is_file()
    assert (tmp_path / "codex" / "hooks.json").is_file()
    assert not (world.home / ".claude").exists()
    assert not (world.home / ".codex").exists()


def test_main_dry_run_writes_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command.main(["--dry-run"], "p") == 0
    out = capsys.readouterr().out
    assert out.startswith("would install prek's")
    # Codex's config.toml has two edits, trust and the environment, which a
    # dry run prints as the one diff a real run writes.
    assert out.count("+++ ") == 4
    assert out.count(f"+++ {world.home / '.codex' / 'config.toml'}\n") == 1
    assert f"gh-shim: would write {gh_shim.bin_dir() / 'gh'}\n" in out
    assert list(world.home.iterdir()) == []
    assert not gh_shim.bin_dir().parent.exists()
    assert world.prek_calls() == []


def test_main_dry_run_prints_one_codex_config_diff(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = world.home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(FOREIGN_CONFIG)
    assert command.main(["--dry-run"], "p") == 0
    out = capsys.readouterr().out
    assert out.count(f"+++ {config}\n") == 1
    assert out.count('+SSH_AUTH_SOCK = ""\n') == 1
    assert '\n+[hooks.state."' in out
    assert config.read_text() == FOREIGN_CONFIG


def test_main_status_is_any_failure(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = world.home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text("[]")
    assert command.main([], "p") == 1
    assert "claude: cannot merge" in capsys.readouterr().err
    assert (world.home / ".codex" / "hooks.json").is_file()


@pytest.mark.parametrize("failing", ["install_git_hooks", "install_gh_shim"])
def test_main_status_keeps_an_early_failure(
    world: World, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(command, failing, fail)
    assert command.main([], "p") == 1
    assert (world.home / ".codex" / "hooks.json").is_file()


def test_main_goes_on_after_a_config_it_cannot_read(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.home / ".claude" / "settings.json").mkdir(parents=True)
    assert command.main([], "p") == 1
    assert "claude: cannot merge" in capsys.readouterr().err
    assert (world.home / ".codex" / "hooks.json").is_file()
    assert (world.home / ".gemini" / "config" / "hooks.json").is_file()


def test_main_twice_changes_nothing_the_second_time(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert shutil.which("op") is None
    assert command.main([], "p") == 0
    capsys.readouterr()
    before = {
        p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for root in (world.home, gh_shim.bin_dir())
        for p in root.rglob("*")
        if p.is_file()
    }
    assert command.main([], "p") == 0
    out = capsys.readouterr().out
    for path in (
        world.home / ".claude" / "settings.json",
        world.home / ".codex" / "hooks.json",
        world.home / ".gemini" / "config" / "hooks.json",
        shim_path(),
    ):
        assert f"{path} is up to date\n" in out
    assert "wrote" not in out
    after = {
        p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for root in (world.home, gh_shim.bin_dir())
        for p in root.rglob("*")
        if p.is_file()
    }
    assert after == before
    assert not [p for p in world.home.rglob("*") if "tbhbdev-backup" in p.name]
    settings = json.loads((world.home / ".claude" / "settings.json").read_text())
    assert settings["env"] == {"SSH_AUTH_SOCK": ""}
    assert "PATH" not in settings["env"]
    codex = tomllib.loads((world.home / ".codex" / "config.toml").read_text())
    assert (
        codex.pop("hooks")["state"].keys()
        == trusted_in(world.home / ".codex" / "config.toml").keys()
    )
    assert codex == {"shell_environment_policy": {"set": {"SSH_AUTH_SOCK": ""}}}


def test_main_keeps_existing_settings_and_codex_keys(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = world.home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"FOO": "1"}, "model": "opus"}))
    config = world.home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        '# keep me\nmodel = "o3"\n\n[shell_environment_policy]\ninherit = "core"\n'
        '\n[shell_environment_policy.set]\nBAR = "2"\n'
    )
    assert command.main([], "p") == 0
    capsys.readouterr()
    merged = json.loads(settings.read_text())
    assert merged["env"] == {"FOO": "1", "SSH_AUTH_SOCK": ""}
    assert merged["model"] == "opus"
    text = config.read_text()
    assert text.startswith('# keep me\nmodel = "o3"\n')
    parsed = tomllib.loads(text)
    assert parsed.pop("hooks")["state"]
    assert parsed == {
        "model": "o3",
        "shell_environment_policy": {
            "inherit": "core",
            "set": {"BAR": "2", "SSH_AUTH_SOCK": ""},
        },
    }


def test_main_twice_with_existing_configs_changes_nothing_the_second_time(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = world.home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"FOO": "1", "SSH_AUTH_SOCK": "/x"}}))
    config = world.home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        '# keep me\nmodel = "o3"\n\n[shell_environment_policy.set]\n'
        'SSH_AUTH_SOCK = "/x"\nBAR = "2"\n'
    )
    assert command.main([], "p") == 0
    capsys.readouterr()
    snapshot = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in [*world.home.rglob("*"), *gh_shim.bin_dir().rglob("*")]
        if p.is_file()
    }
    assert json.loads(settings.read_text())["env"] == {"FOO": "1", "SSH_AUTH_SOCK": ""}
    assert 'SSH_AUTH_SOCK = ""' in config.read_text()
    assert command.main([], "p") == 0
    out = capsys.readouterr().out
    assert f"claude: {settings} is up to date\n" in out
    assert f"codex: 12 hooks already trusted in {config}\n" in out
    assert "wrote" not in out
    assert snapshot == {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in [*world.home.rglob("*"), *gh_shim.bin_dir().rglob("*")]
        if p.is_file()
    }
    backups = [p for p in world.home.rglob("*") if "tbhbdev-backup" in p.name]
    assert len(backups) == 2


USER_CODEX_CONFIG = """# my Codex settings
model = "o3"

[env]
MINE = "1"

[shell_environment_policy]
inherit = "core"

[shell_environment_policy.set]
BAR = "2"

[hooks.state."/elsewhere/hooks.json:pre_tool_use:0:0"]
trusted_hash = "sha256:foreign"
"""


def test_main_twice_trusts_every_codex_hook_and_sets_the_environment(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    config = world.home / ".codex" / "config.toml"
    hooks_path = world.home / ".codex" / "hooks.json"
    config.parent.mkdir()
    config.write_text(USER_CODEX_CONFIG)
    assert command.main([], "p") == 0
    capsys.readouterr()
    first = config.read_bytes()
    parsed = tomllib.loads(first.decode())
    # Every Codex handler of ours has the hash of what is on disk, including the
    # gh poll guard, whose hash is spelled out here rather than computed.
    written = json.loads(hooks_path.read_text())["hooks"]
    ours = [
        (event, at, one)
        for event, groups in written.items()
        for at, group in enumerate(groups)
        for one in group["hooks"]
    ]
    assert len(ours) == 12
    state = parsed["hooks"]["state"]
    for event, at, one in ours:
        assert one["timeout"] == hooks.CODEX_TIMEOUT
        label = {
            "SessionStart": "session_start",
            "SubagentStart": "subagent_start",
            "Stop": "stop",
        }
        label = label.get(event, "post_tool_use" if "Post" in event else "pre_tool_use")
        group = written[event][at]
        body = {
            "event_name": label,
            "hooks": [
                {
                    "type": "command",
                    "command": one["command"],
                    "timeout": 600,
                    "async": False,
                }
            ],
        }
        if "matcher" in group:
            body["matcher"] = group["matcher"]
        compact = json.dumps(body, separators=(",", ":"), sort_keys=True)
        digest = "sha256:" + hashlib.sha256(compact.encode()).hexdigest()
        assert state[f"{hooks_path}:{label}:{at}:0"] == {"trusted_hash": digest}
    poll = f"{hooks_path}:pre_tool_use:1:0"
    assert written["PreToolUse"][1]["hooks"][0]["command"] == marked("guard-gh-poll")
    assert state[poll]["trusted_hash"] == trust_hash(
        "pre_tool_use", "^Bash$", "guard-gh-poll"
    )
    assert trusted_in(config) == {
        "/elsewhere/hooks.json:pre_tool_use:0:0": "sha256:foreign",
        **our_trust(hooks_path),
    }
    # The environment is set, and none of the user's keys is lost.
    assert parsed["shell_environment_policy"] == {
        "inherit": "core",
        "set": {"BAR": "2", "SSH_AUTH_SOCK": ""},
    }
    assert parsed["env"] == {"MINE": "1"}
    assert parsed["model"] == "o3"
    assert first.decode().startswith('# my Codex settings\nmodel = "o3"\n')
    # A second run writes nothing.
    snapshot = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in world.home.rglob("*")
        if p.is_file()
    }
    assert command.main([], "p") == 0
    out = capsys.readouterr().out
    assert f"codex: 12 hooks already trusted in {config}\n" in out
    assert "wrote" not in out
    assert config.read_bytes() == first
    assert snapshot == {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in world.home.rglob("*")
        if p.is_file()
    }
