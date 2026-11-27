"""Agent settings load from a config file under a temporary home."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import pytest

from tbhbdev import userconfig
from tbhbdev.alpha.agent import arguments, model, settings

if TYPE_CHECKING:
    from pathlib import Path

EXAMPLE = """\
[agent]
launch_wait_seconds = 120
agy_timeout = "900s"
kill_grace_seconds = 10
max_run_minutes = 120
sockets_dir = "/example/sockets"

[agent.sandbox]
audit_dir = ".local/state/example-audit"

[agent.sandbox.path]
linux = [".local/bin", "/usr/local/bin", "/usr/bin", "/bin"]
macos = [".local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]

[agent.codex]
web_search = { build = "live", review = "cached" }

[agent.credentials.agent]
gh = "example-vault/example-gh/token"
docker = "example-vault/example-docker/config"
reviews_app_client_id = "example-vault/example-app/client_id"
reviews_app_pem = "example-vault/example-app/private_key"
signing = "example-vault/example-signing/private_key"

[agent.claude]
default_profile = "primary"

[agent.claude.profiles.primary]
token = "example/primary/token"

[agent.claude.profiles.secondary]
token = "example/secondary/token"

[[agent.host]]
name = "example-linux"
os = "linux"
shell = "bash"
account = "agent"
accounts = ["agent"]

[[agent.host]]
name = "example-macos"
os = "macos"
shell = "zsh"
account = "agent"
accounts = ["agent"]
"""


def test_loads_the_example_from_the_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "example-home"
    (home / ".config" / "tbhbdev").mkdir(parents=True)
    (home / ".config" / "tbhbdev" / "config.toml").write_text(EXAMPLE)
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(home))
    loaded = settings.load()
    assert loaded.launch_wait_seconds == 120
    assert loaded.agy_timeout_seconds == 900
    assert loaded.codex == model.Codex(model.WebSearch.LIVE, model.WebSearch.CACHED)
    assert [h.name for h in loaded.hosts] == ["example-linux", "example-macos"]
    assert loaded.claude.default_profile == "primary"
    assert (
        loaded.credentials[0].reviews_app_pem == "example-vault/example-app/private_key"
    )


def test_missing_file_names_the_config(config_file: Path) -> None:
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == (
        f"{config_file}: config cannot be read: "
        f"{config_file} doesn't exist; create it and set [agent]"
    )


def test_bad_table_names_the_config(config_file: Path) -> None:
    config_file.write_text('agent = "invalid"\n')
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == (
        f"{config_file}: config cannot be read: {config_file}: agent must be a table"
    )


@pytest.mark.parametrize("text", [b"[agent", b"\xff"])
def test_invalid_file_is_a_configuration_error(config_file: Path, text: bytes) -> None:
    config_file.write_bytes(text)
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value).startswith(f"{config_file}: config cannot be read: ")


def test_unreadable_file_is_a_configuration_error(config_file: Path) -> None:
    config_file.mkdir()
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value).startswith(f"{config_file}: config cannot be read: ")


def test_absent_table_is_an_error(config_file: Path) -> None:
    config_file.write_text('[loop]\nhosts = ["x"]\n')
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == f"{config_file}: [agent] table is required"


def test_invalid_setting_names_the_file_table_and_key(config_file: Path) -> None:
    config_file.write_text(
        EXAMPLE.replace('audit_dir = ".local/state/example-audit"\n', "")
    )
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == f"{config_file}: [agent.sandbox] audit_dir is required"


def test_unknown_key_names_the_full_table_path(config_file: Path) -> None:
    config_file.write_text(
        EXAMPLE.replace('shell = "bash"\n', 'shell = "bash"\ncredentials = "connect"\n')
    )
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert (
        str(caught.value) == f"{config_file}: [[agent.host]] credentials not recognized"
    )


def test_loading_reads_only_the_config(config_file: Path, tmp_path: Path) -> None:
    config_file.write_text(EXAMPLE)
    before = sorted(p.name for p in tmp_path.iterdir())
    settings.load()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_duration() -> None:
    assert arguments.duration("1h") == 3600

    with pytest.raises(argparse.ArgumentTypeError) as caught:
        arguments.duration("bad")
    assert (
        str(caught.value)
        == "must be bare seconds or a number ending in s, m or h, up to 7 days"
    )


def test_command_help_contract() -> None:
    help_text = arguments.parser("example agent").format_help()
    assert (
        "{exec,wait,status,kill,clean,keep,log,diff,send,signing-parity}" in help_text
    )
    assert "launch a headless run" in help_text
    assert "steer an active Codex run" in help_text


def test_a_relative_worker_path_names_the_config(config_file: Path) -> None:
    config_file.write_text(EXAMPLE + '\n[agent.worker]\npath = "bin/worker"\n')
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == (
        f"{config_file}: [agent.worker] path must be an absolute path"
    )


def test_the_worker_path_loads_from_the_config(config_file: Path) -> None:
    config_file.write_text(EXAMPLE + '\n[agent.worker]\npath = "/opt/worker"\n')
    assert settings.load().worker_path == "/opt/worker"
    config_file.write_text(EXAMPLE)
    assert settings.load().worker_path is None
