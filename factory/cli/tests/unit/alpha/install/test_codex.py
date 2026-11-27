"""Tests for install codex."""

import json
import os
import stat
import tomllib
from typing import TYPE_CHECKING

import pytest
from unit.alpha.install.expectations import (
    FOREIGN_CONFIG,
    THEIRS,
    marked,
    our_trust,
    trust_hash,
    trusted_in,
)

from tbhbdev.alpha.install import config as harness_config
from tbhbdev.alpha.install import files

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def codex_env(isolated_home: Path) -> tuple[dict[str, str], Path]:
    codex = isolated_home / "codex-home"
    return {"CODEX_HOME": str(codex)}, codex


@pytest.fixture
def loose_umask() -> Iterator[None]:
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.usefixtures("loose_umask")
def test_install_config_codex_trusts_each_hook_in_a_new_config_toml(
    isolated_home: Path,
) -> None:
    env, codex = codex_env(isolated_home)
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    config = codex / "config.toml"
    assert trusted_in(config) == our_trust(codex / "hooks.json")
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert sorted(p.name for p in codex.iterdir()) == ["config.toml", "hooks.json"]


def test_install_config_codex_keys_use_the_absolute_hooks_path(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(isolated_home)
    assert (
        harness_config.install_config("codex", {"CODEX_HOME": "rel"}, dry_run=False)
        == 0
    )
    keys = trusted_in(isolated_home / "rel" / "config.toml")
    assert keys == our_trust(isolated_home / "rel" / "hooks.json")
    assert all(key.startswith(f"{isolated_home}/rel/hooks.json:") for key in keys)


def test_install_config_codex_trusts_only_its_own_among_foreign_groups(
    isolated_home: Path,
) -> None:
    env, codex = codex_env(isolated_home)
    hooks = codex / "hooks.json"
    old_ours = {"type": "command", "command": marked("guard-op")}
    codex.mkdir()
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "^Bash$", "hooks": [THEIRS]},
                        {"matcher": "x", "hooks": [THEIRS, old_ours, THEIRS]},
                        {"matcher": "^Bash$", "hooks": [THEIRS]},
                    ],
                    "Stop": [{"hooks": [THEIRS]}],
                }
            }
        )
    )
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    written = json.loads(hooks.read_text())["hooks"]["PreToolUse"]
    assert [g.get("matcher") for g in written] == [
        "^Bash$",
        "^Bash$",
        "^Bash$",
        "^Bash$",
        "^apply_patch$",
        "^(Bash|shell)$",
        "apply_patch",
        "^Bash$",
        "x",
        "^Bash$",
    ]
    assert [g["hooks"] for g in written] == [
        [THEIRS],
        *(g["hooks"] for g in written[1:8]),
        [THEIRS, THEIRS],
        [THEIRS],
    ]
    expected = {
        key: digest
        for key, digest in our_trust(hooks).items()
        if ":pre_tool_use:" not in key and ":stop:" not in key
    }
    expected |= {
        f"{hooks}:stop:1:0": trust_hash("stop", None, "run-stop-hook codex"),
        f"{hooks}:pre_tool_use:1:0": trust_hash(
            "pre_tool_use", "^Bash$", "guard-gh-pr"
        ),
        f"{hooks}:pre_tool_use:2:0": trust_hash(
            "pre_tool_use", "^Bash$", "guard-gh-poll"
        ),
        f"{hooks}:pre_tool_use:3:0": trust_hash(
            "pre_tool_use", "^Bash$", "guard-scripts"
        ),
        f"{hooks}:pre_tool_use:4:0": trust_hash(
            "pre_tool_use", "^apply_patch$", "guard-scripts"
        ),
        f"{hooks}:pre_tool_use:5:0": trust_hash(
            "pre_tool_use", "^(Bash|shell)$", "guard-op"
        ),
        f"{hooks}:pre_tool_use:6:0": trust_hash(
            "pre_tool_use", "apply_patch", "guard-op"
        ),
        f"{hooks}:pre_tool_use:7:0": trust_hash(
            "pre_tool_use", "^Bash$", "guard-gh-config-dir"
        ),
    }
    assert trusted_in(codex / "config.toml") == expected


def test_install_config_codex_keeps_everything_else_in_config_toml(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(files, "stamp", lambda: "T")
    env, codex = codex_env(isolated_home)
    config = codex / "config.toml"
    codex.mkdir()
    config.write_text(FOREIGN_CONFIG)
    config.chmod(0o644)
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    backup = codex / "config.toml.tbhbdev-backup.T"
    assert capsys.readouterr().out.endswith(
        f"codex: trusted 12 hooks in {config}; the old one is {backup}\n"
    )
    assert backup.read_text() == FOREIGN_CONFIG
    assert stat.S_IMODE(config.stat().st_mode) == 0o644
    assert stat.S_IMODE(backup.stat().st_mode) == 0o644
    new = config.read_text()
    assert set(FOREIGN_CONFIG.splitlines()) <= set(new.splitlines())
    expected = tomllib.loads(FOREIGN_CONFIG)
    expected["shell_environment_policy"] = {"set": {"SSH_AUTH_SOCK": ""}}
    expected["hooks"]["state"] |= {
        key: {"trusted_hash": digest}
        for key, digest in our_trust(codex / "hooks.json").items()
    }
    assert tomllib.loads(new) == expected


def test_install_config_codex_twice_changes_nothing_the_second_time(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, codex = codex_env(isolated_home)
    config = codex / "config.toml"
    codex.mkdir()
    config.write_text(FOREIGN_CONFIG)
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    capsys.readouterr()
    first = config.read_bytes()
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    assert capsys.readouterr().out == (
        f"codex: {codex / 'hooks.json'} is up to date\n"
        f"codex: 12 hooks already trusted in {config}\n"
    )
    assert config.read_bytes() == first
    assert sorted(p.name for p in codex.iterdir()) == [
        "config.toml",
        "config.toml.tbhbdev-backup." + stamp_of(codex),
        "hooks.json",
    ]


def stamp_of(codex: Path) -> str:
    """The time suffix of the one backup in CODEX."""
    (backup,) = codex.glob("config.toml.tbhbdev-backup.*")
    return backup.name.rsplit(".", 1)[1]


def test_install_config_codex_trusts_again_when_only_config_toml_was_lost(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, codex = codex_env(isolated_home)
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    (codex / "config.toml").unlink()
    capsys.readouterr()
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    assert capsys.readouterr().out == (
        f"codex: {codex / 'hooks.json'} is up to date\n"
        f"codex: trusted 12 hooks in {codex / 'config.toml'}\n"
    )
    assert trusted_in(codex / "config.toml") == our_trust(codex / "hooks.json")


def test_install_config_codex_drops_the_entry_of_a_hook_that_moved(
    isolated_home: Path,
) -> None:
    env, codex = codex_env(isolated_home)
    hooks = codex / "hooks.json"
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    config = codex / "config.toml"
    first = trusted_in(config)
    # A foreign group lands ahead of ours, so each PreToolUse hook moves down one,
    # and the foreign hook now sits where our first one was.
    data = json.loads(hooks.read_text())
    data["hooks"]["PreToolUse"].insert(0, {"matcher": "^Bash$", "hooks": [THEIRS]})
    hooks.write_text(json.dumps(data))
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    moved = trusted_in(config)
    assert f"{hooks}:pre_tool_use:0:0" not in moved
    assert moved[f"{hooks}:pre_tool_use:1:0"] == first[f"{hooks}:pre_tool_use:0:0"]
    assert moved[f"{hooks}:pre_tool_use:2:0"] == first[f"{hooks}:pre_tool_use:1:0"]
    assert moved[f"{hooks}:pre_tool_use:3:0"] == first[f"{hooks}:pre_tool_use:2:0"]
    assert moved[f"{hooks}:pre_tool_use:4:0"] == first[f"{hooks}:pre_tool_use:3:0"]
    # The foreign hook's own entry, whatever its hash, is left alone.
    config.write_text(
        config.read_text() + f'\n[hooks.state."{hooks}:pre_tool_use:0:0"]\n'
        'trusted_hash = "sha256:theirs"\n'
    )
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    assert trusted_in(config)[f"{hooks}:pre_tool_use:0:0"] == "sha256:theirs"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("[hooks\n", "it is not valid TOML ("),
        ("hooks = 3\n", "its hooks is not a table"),
        ("[hooks]\nstate = 1\n", "its hooks.state is not a table"),
    ],
)
def test_install_config_codex_reports_a_config_toml_it_cannot_edit(
    isolated_home: Path, capsys: pytest.CaptureFixture[str], text: str, reason: str
) -> None:
    env, codex = codex_env(isolated_home)
    config = codex / "config.toml"
    codex.mkdir()
    config.write_text(text)
    assert harness_config.install_config("codex", env, dry_run=False) == 1
    captured = capsys.readouterr()
    assert captured.out == f"codex: wrote {codex / 'hooks.json'}\n"
    prefix = f"tbhbdev alpha install: codex: cannot trust hooks in {config}: {reason}"
    assert captured.err.startswith(prefix)
    assert captured.err.count("\n") == 1
    assert config.read_text() == text
    assert sorted(p.name for p in codex.iterdir()) == ["config.toml", "hooks.json"]


def test_install_config_codex_trusts_a_config_toml_whose_environment_cannot_merge(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, codex = codex_env(isolated_home)
    config = codex / "config.toml"
    codex.mkdir()
    config.write_text("shell_environment_policy = 3\n")
    assert harness_config.install_config("codex", env, dry_run=False) == 1
    assert trusted_in(config) == our_trust(codex / "hooks.json")
    assert tomllib.loads(config.read_text())["shell_environment_policy"] == 3
    assert "its shell_environment_policy is not a table" in capsys.readouterr().err


def test_install_config_codex_reports_a_config_toml_it_cannot_read(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, codex = codex_env(isolated_home)
    config = codex / "config.toml"
    config.mkdir(parents=True)
    assert harness_config.install_config("codex", env, dry_run=False) == 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha install: codex: cannot trust hooks in {config}: it cannot "
        f"be read ([Errno 21] Is a directory: {str(config)!r})\n"
    )


def test_install_config_codex_reports_a_config_toml_it_cannot_write(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, codex = codex_env(isolated_home)
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    (codex / "config.toml").unlink()
    codex.chmod(0o500)
    capsys.readouterr()
    try:
        assert harness_config.install_config("codex", env, dry_run=False) == 1
        err = capsys.readouterr().err
        assert err.startswith(
            f"tbhbdev alpha install: codex: cannot trust hooks in "
            f"{codex / 'config.toml'}: [Errno 13] Permission denied"
        )
    finally:
        codex.chmod(0o700)
    assert not (codex / "config.toml").exists()


def test_install_config_codex_dry_run_prints_both_diffs_and_writes_nothing(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, codex = codex_env(isolated_home)
    assert harness_config.install_config("codex", env, dry_run=True) == 0
    out = capsys.readouterr().out
    assert f"+++ {codex / 'hooks.json'}\n" in out
    assert f"+++ {codex / 'config.toml'}\n" in out
    assert f'+[hooks.state."{codex / "hooks.json"}:session_start:0:0"]\n' in out
    assert not codex.exists()


def test_install_config_codex_dry_run_diffs_against_an_existing_config_toml(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, codex = codex_env(isolated_home)
    codex.mkdir()
    (codex / "config.toml").write_text(FOREIGN_CONFIG)
    assert harness_config.install_config("codex", env, dry_run=True) == 0
    out = capsys.readouterr().out
    _, _, diff = out.partition(f"--- {codex / 'config.toml'}\n")
    assert diff.startswith(f"+++ {codex / 'config.toml'}\n@@ -")
    assert "@@ -0,0" not in diff
    assert '\n trusted_hash = "sha256:foreign"\n' in diff
    assert '\n+[hooks.state."' in diff
    assert (codex / "config.toml").read_text() == FOREIGN_CONFIG
    assert sorted(p.name for p in codex.iterdir()) == ["config.toml"]


def test_install_config_codex_dry_run_with_trust_up_to_date_prints_no_diff(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, codex = codex_env(isolated_home)
    assert harness_config.install_config("codex", env, dry_run=False) == 0
    capsys.readouterr()
    assert harness_config.install_config("codex", env, dry_run=True) == 0
    assert capsys.readouterr().out == (
        f"codex: {codex / 'hooks.json'} is up to date\n"
        f"codex: 12 hooks already trusted in {codex / 'config.toml'}\n"
    )
