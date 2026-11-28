# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the host fitness checks, with every input faked."""

# gazelle:include_dep //factory/cli/src/tbhbdev/alpha:command_modules

import json
import os
import socket
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from fake_tool import write_fake

from tbhbdev import userconfig
from tbhbdev.alpha import command as alpha_command
from tbhbdev.alpha.agent.model import Host, Os
from tbhbdev.alpha.host import command as hc

if TYPE_CHECKING:
    from pathlib import Path

BASE = "2026-01-01T00:00:00Z"
LATE = "2026-01-01T07:00:00Z"
PROBE = "{} 4 64000000 60000000 0 0 - -\n2 1\n"


def git(repo: Path, *args: str, when: str = BASE) -> None:
    env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    who = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "-C", str(repo), *who, *args], check=True, env=env)


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An origin with one commit and a clone of it, with no user Git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "mise.toml").write_text('[tools]\nruff = "1.2.0"\n')
    git(origin, "add", ".")
    git(origin, "commit", "-q", "-m", "x")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


@pytest.mark.parametrize(
    ("extra", "when", "ok"), [(3, BASE, True), (60, BASE, False), (1, LATE, False)]
)
def test_clone_lag(repos: tuple[Path, Path], extra: int, when: str, ok: bool) -> None:
    origin, clone = repos
    for _ in range(extra):
        git(origin, "commit", "-q", "--allow-empty", "-m", "x", when=when)
    assert hc.clone_check(clone, fetch=False).value.startswith("0 commits")
    result = hc.clone_check(clone, fetch=True)
    assert (result.ok, result.repair) == (ok, "none" if ok else "fetch-clone")
    assert result.value.startswith(f"{extra} commits")


@pytest.mark.usefixtures("repos")
def test_clone_unusable(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", str(tmp_path / "bare"))
    for path in (tmp_path / "none", tmp_path / "bare"):
        result = hc.clone_check(path, fetch=True)
        assert (result.ok, result.repair) == (False, "fetch-clone")


@pytest.mark.parametrize(("have", "ok"), [("1.2.0", True), ("1.3.0", False)])
def test_tools(repos: tuple[Path, Path], tmp_path: Path, have: str, ok: bool) -> None:
    clone = repos[1]
    (clone / "mise.toml").write_text('[tools]\nruff = "9.9.9"\n')  # only origin counts
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    write_fake(home / ".local/bin/ruff", "", version=have)
    result = hc.tools_check(clone, home)
    assert (result.ok, result.repair) == (ok, "none" if ok else "install-tools")
    assert not hc.tools_check(tmp_path / "none", home).ok


@pytest.mark.parametrize(
    ("readings", "failing"),
    [
        ((50, 50, 79), None),
        ((14, 50, 0), ("disk", "clean-disk")),
        ((50, 14, 0), ("disk", "clean-disk")),
        ((50, 50, 85), ("tmp-inodes", "clean-tmp")),
    ],
)
def test_disk_and_inodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    readings: tuple[int, int, int],
    failing: tuple[str, str] | None,
) -> None:
    (tmp_path / ".cache/bazel").mkdir(parents=True)
    free, bazel, used = readings

    def statvfs(path: object) -> SimpleNamespace:
        left = bazel if str(path).endswith(".cache/bazel") else free
        return SimpleNamespace(
            f_blocks=100, f_bavail=left, f_files=100, f_ffree=100 - used
        )

    monkeypatch.setattr(os, "statvfs", statvfs)
    found = [hc.disk_check(tmp_path), hc.inode_check()]
    bad = [(c.name, c.repair) for c in found if not c.ok]
    assert bad == ([failing] if failing else [])


def probe(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    run = Mock(return_value=SimpleNamespace(stdout=text))
    monkeypatch.setattr(hc.tool_pins, "tool_command", run)


def test_load_and_admitted_inform_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc.settings, "load", Mock(side_effect=userconfig.ConfigError))
    probe(monkeypatch, PROBE.format(99))
    load, admitted = hc.load_checks()
    assert load.ok
    assert "load 99 >= limit 4" in load.value
    assert (admitted.ok, admitted.value) == (True, "2")
    here = socket.gethostname().partition(".")[0]
    host = Host(here, Os.LINUX, "sh", "a", ("a",))
    other = replace(host, name="other")
    loaded = SimpleNamespace(hosts=(other, host), run_memory_mib=1)  # one is ours
    monkeypatch.setattr(hc.settings, "load", lambda: loaded)
    probe(monkeypatch, PROBE.format(1))
    assert hc.load_checks()[0].value == "within limits"


def test_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc.version, "line", lambda: "tbhbdev 0.1.0 (abc123)")
    assert hc.cli_check().value == "abc123"
    monkeypatch.setattr(hc.version, "line", Mock(side_effect=ValueError))
    result = hc.cli_check()
    assert (result.ok, result.repair) == (False, "reinstall-cli")
    monkeypatch.setattr(hc.version, "line", lambda: "tbhbdev 0.1.0 (abc123)")
    monkeypatch.setitem(
        alpha_command.COMMAND_MODULES, "gone", ("tbhbdev.alpha.gone", "x")
    )
    assert "tbhbdev.alpha.gone" in hc.cli_check().value


@pytest.mark.parametrize(("fit", "code"), [(True, 0), (False, 1)])
def test_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fit: bool,
    code: int,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    found = [
        hc.Check("c0", ok=True, value="v", limit="l"),
        hc.Check("c1", ok=fit, value="v", limit="l", repair="clean-tmp"),
    ]
    run = Mock(return_value=found)
    monkeypatch.setattr(hc, "run_all", run)
    flags = ["--json"] if fit else []
    assert hc.main(["check", "--no-fetch", *flags], "p") == code
    assert run.call_args.kwargs == {"fetch": False}
    state = tmp_path / ".local/state/tbhbdev/host-fitness.json"
    written = json.loads(state.read_text())
    assert written["fit"] is fit
    assert [c["name"] for c in written["checks"]] == ["c0", "c1"]
    assert {"checked_at", "revision", "seconds"} <= written.keys()
    out = capsys.readouterr().out
    assert (json.loads(out) == written) if fit else "c1" in out


def test_exit_two_when_a_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hc, "run_all", Mock(side_effect=OSError("boom")))
    assert hc.main(["check"], "p") == 2
    assert not (tmp_path / ".local").exists()
    with pytest.raises(SystemExit):
        hc.main(["nope"], "p")


def test_run_all_runs_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    names = ["cli_check", "clone_check", "tools_check", "disk_check", "inode_check"]
    for name in names:
        monkeypatch.setattr(hc, name, Mock(return_value=name))
    monkeypatch.setattr(hc, "load_checks", Mock(return_value=["load"]))
    assert hc.run_all(Mock(), Mock(), fetch=False) == [*names, "load"]
