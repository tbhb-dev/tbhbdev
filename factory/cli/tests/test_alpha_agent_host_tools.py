"""Host paths and script interpreters are resolved before execution."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from agent_example import settings

from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.model import Invalid, Os


def script(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize("relative", [False, True])
def test_executable_links_follow_directory_aliases_after_parent_components(
    tmp_path: Path, relative: bool
) -> None:
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    script(release / "bin/codex", "binary")
    directory = tmp_path / "bin"
    directory.mkdir()
    current = tmp_path / "current"
    current.symlink_to(
        release.relative_to(tmp_path) if relative else release, target_is_directory=True
    )
    alias = directory / "codex"
    alias.symlink_to("../current/bin/codex" if relative else current / "bin/codex")
    assert host_tools.executable_links(alias) == (str(alias), str(current))


def test_executable_link_walk_refuses_missing_paths(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        host_tools.executable_links(tmp_path / "missing")


@pytest.mark.parametrize("os", Os)
def test_host_path_is_fixed_and_scoped(
    os: Os, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = settings()
    configured = dataclasses.replace(
        original,
        sandbox=dataclasses.replace(original.sandbox, host_path=("/example/bin",)),
    )
    monkeypatch.setenv("PATH", "/untrusted")
    entries = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
    if os is Os.MACOS:
        entries += ("/opt/homebrew/bin", "/usr/local/bin")
    assert host_tools.search_path(configured, os) == (*entries, "/example/bin")
    directories = ":".join((*entries, "/example/bin"))
    executable = script(tmp_path / "git", "binary")

    def which(name: str, *, path: str) -> str | None:
        assert path == directories
        return executable if name == "git" else None

    monkeypatch.setattr(host_tools.shutil, "which", which)
    before = host_tools.search()
    with host_tools.configured(configured, os):
        assert host_tools.search() == directories
        assert Path(host_tools.resolve("git")).is_absolute()
        with pytest.raises(Invalid) as caught:
            host_tools.resolve("missing")
        assert str(caught.value) == (
            f"host executable missing was not found on PATH {directories}"
        )
    assert host_tools.search() == before


def test_resolution_follows_symlinks_and_refuses_missing(tmp_path: Path) -> None:
    target = script(tmp_path / "target", "#!/bin/sh\nexit 0\n")
    (tmp_path / "tool").symlink_to(target)
    assert host_tools.resolve("tool", str(tmp_path)) == target
    assert host_tools.resolve("tool", str(tmp_path), follow_symlinks=False) == str(
        tmp_path / "tool"
    )
    with pytest.raises(Invalid) as caught:
        host_tools.resolve("missing", str(tmp_path))
    assert (
        str(caught.value) == f"host executable missing was not found on PATH {tmp_path}"
    )


@pytest.mark.parametrize("indirect", [False, True])
def test_parent_commands_refuse_mise_shims(tmp_path: Path, indirect: bool) -> None:
    directory = tmp_path / "mise" / "shims"
    directory.mkdir(parents=True)
    target = script(tmp_path / "mise-binary", "binary")
    shim = directory / "codex"
    shim.symlink_to(target)
    search = directory
    if indirect:
        search = tmp_path / "bin"
        search.mkdir()
        (search / "codex").symlink_to(shim)
    with pytest.raises(Invalid) as caught:
        host_tools.resolve("codex", str(search))
    assert str(caught.value) == (
        "host executable codex is a mise shim; configure its fixed install directory"
    )


def test_preserved_lookup_makes_relative_search_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(tmp_path / "target", "binary")
    (tmp_path / "tool").symlink_to("target")
    monkeypatch.chdir(tmp_path)
    assert host_tools.resolve("tool", ".", follow_symlinks=False) == str(
        tmp_path / "tool"
    )


def test_account_path_uses_the_run_home() -> None:
    paths = host_tools.account_path("/example/agent")
    assert paths[:4] == tuple(
        f"/example/agent/{tail}"
        for tail in (".local/bin", ".local/gh-bin", ".bun/bin", ".npm-global/bin")
    )
    assert paths[4:6] == ("/usr/local/bin", "/opt/homebrew/bin")
    assert paths[6:] == ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
