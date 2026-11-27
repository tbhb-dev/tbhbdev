"""Tests for install session."""

import io
import json
import os
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from unit.alpha.install.conftest import World, make_repo

from tbhbdev.alpha import gh_shim
from tbhbdev.alpha.install import session

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.install.model import Json


def feed(monkeypatch: pytest.MonkeyPatch, data: Json) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))


def test_session_start_installs_only_the_payloads_git_hooks(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    feed(monkeypatch, {"cwd": str(world.repo)})
    assert session.session_start(os.environ) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("prek installed pre-commit\nprek installed pre-push\n")
    assert err.endswith(f"git hooks in {world.hooks}\n")
    assert (world.hooks / "commit-msg").is_file()
    assert list(world.home.iterdir()) == []


def test_session_start_reports_a_failure_on_stderr_and_exits_0(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.bin / "prek.fail").write_text("")
    feed(monkeypatch, {"cwd": str(world.repo)})
    assert session.session_start(os.environ) == 0
    assert capsys.readouterr() == (
        "",
        (
            "prek: boom\ntbhbdev alpha install: error: prek install --hook-type "
            "pre-commit --hook-type pre-push failed\n"
        ),
    )


def test_session_start_reports_a_crash_in_one_line_and_exits_0(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(*_: object, **__: object) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(session, "install_git_hooks", crash)
    feed(monkeypatch, {"cwd": str(world.repo)})
    assert session.session_start(os.environ) == 0
    assert capsys.readouterr() == (
        "",
        "tbhbdev alpha install: error: OSError: [Errno 28] No space left on device\n",
    )


def test_session_start_outside_tbhb_dev_is_silent(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = make_repo(tmp_path / "other", "https://github.com/someone/x")
    feed(monkeypatch, {"cwd": str(other)})
    assert session.session_start(os.environ) == 0
    assert capsys.readouterr() == ("", "")
    assert world.prek_calls() == []


def export_line() -> str:
    return f'export PATH={shlex.quote(str(gh_shim.bin_dir()))}":$PATH"'


def test_export_shim_path_without_an_env_file(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    del world
    session.export_shim_path({})
    session.export_shim_path({"CLAUDE_ENV_FILE": ""})
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, "{line}\n"),
        ("", "{line}\n"),
        ("export A=1\n", "export A=1\n{line}\n"),
        ("export A=1", "export A=1\n{line}\n"),
    ],
)
def test_export_shim_path_appends_once(
    world: World,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    before: str | None,
    after: str,
) -> None:
    del world
    path = tmp_path / "env.sh"
    if before is not None:
        path.write_text(before)
    env = {"CLAUDE_ENV_FILE": str(path)}
    session.export_shim_path(env)
    session.export_shim_path(env)
    assert path.read_text() == after.format(line=export_line())
    assert capsys.readouterr() == ("", "")


def test_export_shim_path_puts_the_shim_first_when_sourced(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del world
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data dir's"))
    path = tmp_path / "env.sh"
    session.export_shim_path({"CLAUDE_ENV_FILE": str(path)})
    done = subprocess.run(
        ["/bin/sh", "-c", '. "$1"; printf %s "$PATH"', "sh", str(path)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == f"{gh_shim.bin_dir()}:/usr/bin:/bin"


def test_export_shim_path_reports_a_failure(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del world
    path = tmp_path / "env.sh"
    path.mkdir()
    session.export_shim_path({"CLAUDE_ENV_FILE": str(path)})
    assert capsys.readouterr() == (
        "",
        (
            "tbhbdev alpha install: cannot put the gh shim on PATH: "
            f"[Errno 21] Is a directory: {str(path)!r}\n"
        ),
    )


def test_session_start_exports_the_shim_path_on_stderr_only(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "env.sh"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(path))
    other = make_repo(tmp_path / "other", "https://github.com/someone/x")
    feed(monkeypatch, {"cwd": str(other)})
    assert session.session_start(os.environ) == 0
    assert capsys.readouterr() == ("", "")
    assert path.read_text() == f"{export_line()}\n"
    assert world.prek_calls() == []
