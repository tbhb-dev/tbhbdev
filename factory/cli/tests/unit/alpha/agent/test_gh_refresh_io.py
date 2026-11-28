"""Exercise the host-side GitHub token refresher through Git and gh."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from typing import TYPE_CHECKING

import agent_host_example as ex
import pytest

from tbhbdev.alpha.agent import credentials, gh_refresh, worktree_clone
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from pathlib import Path


def fill(clone: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(clone), "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=False, timeout=10,
    )  # fmt: skip


def test_credential_helper_refresh_symlink_safety_and_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, clone, tools = tmp_path / "m", tmp_path / "c", tmp_path / "bin"
    gh_dir, outside, hy = tmp_path / "gh", tmp_path / "out", credentials.hosts_yaml
    for path in (tools, gh_dir, outside):
        path.mkdir()
    subprocess.run(["git", "init", "--bare", str(mirror)], check=True, timeout=10)
    worktree_clone.clone_run(mirror, clone, "https://github.com/o/r.git", Profile.BUILD)
    gh = tools / "gh"
    gh.write_text(
        "#!/bin/sh\necho username=x\n"
        'sed -n "s/.*oauth_token: /password=/p" "$GH_CONFIG_DIR/hosts.yml"\n'
    )
    gh.chmod(0o700)
    (gh_dir / "hosts.yml").write_text(hy("github.com", "old"))
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh_dir))

    now, fail_next = [1000.0], [False]
    minted: list[tuple[str, dict[str, str]]] = []

    def mint(_refs: object, repo: str, perms: dict[str, str]) -> str:
        if fail_next[0]:
            raise Invalid(credentials.FRESH_REFUSAL)
        minted.append((repo, perms))
        return f"fresh-{len(minted)}"

    monkeypatch.setattr(credentials, "pull_requests_token", mint)
    req, refs = ex.run_request(), ex.pull_requests_app_settings().credentials[0]
    tok = credentials.StoredToken("old", 1000.0)
    with gh_refresh.serve(gh_dir, req, refs, tok, clock=lambda: now[0]):
        now[0] = 1060.0
        assert ("password=old\n" in fill(clone).stdout, minted) == (True, [])
        now[0] = 8200.0
        got = fill(clone)
        assert (got.returncode, "password=fresh-1\n" in got.stdout) == (0, True)
        assert minted == [("tbhb-dev/example-repo", credentials.PUSH_PERMISSIONS)]
        assert (gh_dir / "hosts.yml").read_text() == hy("github.com", "fresh-1")
        now[0] = 8260.0
        assert ("password=fresh-1\n" in fill(clone).stdout, len(minted)) == (True, 1)
        moved = gh_dir.rename(tmp_path / "moved")
        gh_dir.symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv("GH_CONFIG_DIR", str(moved))
        now[0] = 15400.0
        gh_refresh.refresh_if_served()
        assert not (outside / "hosts.yml").exists()
        assert (moved / "hosts.yml").read_text() == hy("github.com", "fresh-2")
        fail_next[0], now[0] = True, 22600.0
        refused = fill(clone)
        assert refused.returncode != 0
        assert credentials.FRESH_REFUSAL in refused.stderr
    assert not (moved / gh_refresh.SOCKET_NAME).exists()


def test_proactive_refresh_and_missing_or_dead_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    gh_refresh.refresh_if_served()
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    gh_refresh.refresh_if_served()
    with socket.socket(socket.AF_UNIX) as dead:
        dead.bind(str(tmp_path / gh_refresh.SOCKET_NAME))
    with pytest.raises(Invalid, match=credentials.FRESH_REFUSAL):
        gh_refresh.refresh_if_served()

    hosts, want = tmp_path / "hosts.yml", credentials.hosts_yaml("github.com", "pro")
    hosts.write_text(credentials.hosts_yaml("github.com", "old"))
    monkeypatch.setattr(credentials, "pull_requests_token", ex.constant("pro"))
    req, refs = ex.run_request(), ex.pull_requests_app_settings().credentials[0]
    tok = credentials.StoredToken("old", 1000.0)
    with gh_refresh.serve(tmp_path, req, refs, tok, clock=lambda: 8200.0):
        end = time.monotonic() + 5
        while time.monotonic() < end and hosts.read_text() != want:
            time.sleep(0.01)
    assert hosts.read_text() == want
