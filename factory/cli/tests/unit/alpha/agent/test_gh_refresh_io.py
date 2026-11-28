"""Exercise the host-side GitHub token refresher through Git and gh."""

from __future__ import annotations

import os
import subprocess
import time
from typing import TYPE_CHECKING

import yaml

from tbhbdev.alpha.agent import (
    credentials,
    gh_refresh,
    runfiles,
    worktree_clone,
    worktree_git,
)
from tbhbdev.alpha.agent.model import (
    Credentials,
    Harness,
    Invalid,
    Profile,
    RunKind,
    RunRequest,
)

if TYPE_CHECKING:
    from pathlib import Path

HOUR = 3600
PUSH = {"contents": "write", "pull_requests": "write", "workflows": "write"}


def build_request() -> RunRequest:
    return RunRequest(
        RunKind.EXEC, "tbhb-dev/example-repo", None, Harness.CODEX,
        "gpt-5.4", "high", Profile.BUILD, None, None, "agent", 900,
        None, None, None, unsandboxed=True, authorized_by=None,
        startup_deadline_seconds=60,
    )  # fmt: skip


def app_references() -> Credentials:
    return Credentials(
        "agent", None, None, None, None, "signing", "example/client", "example/pem"
    )


def hosts_token(gh_dir: Path) -> str:
    data = yaml.safe_load((gh_dir / "hosts.yml").read_text())
    return str(data["github.com"]["oauth_token"])


def setup_push(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    mirror, clone, tools = tmp_path / "mirror", tmp_path / "clone", tmp_path / "bin"
    mirror.mkdir()
    tools.mkdir()
    worktree_git.git(mirror, "init", "--bare", "--template=")
    worktree_clone.clone_run(
        mirror, clone, "https://github.com/tbhb-dev/example-repo.git", Profile.BUILD
    )
    gh = tools / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'tok=$(sed -n "s/^[[:space:]]*oauth_token:[[:space:]]*//p"'
        ' "$GH_CONFIG_DIR/hosts.yml")\n'
        'printf "username=x-access-token\\npassword=%s\\n" "$tok"\n'
    )
    gh.chmod(0o700)
    gh_dir = runfiles.private_dir(tmp_path / "gh")
    runfiles.atomic(
        gh_dir / "hosts.yml", credentials.hosts_yaml("github.com", "expired-launch")
    )
    env = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "GH_CONFIG_DIR": str(gh_dir),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return clone, gh_dir, env


def fill(clone: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(clone), "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        env=env, capture_output=True, text=True, check=False, timeout=10,
    )  # fmt: skip


def test_build_push_credential_helper_refreshes_expired_and_reports_refusal(
    tmp_path: Path,
) -> None:
    clone, gh_dir, env = setup_push(tmp_path)
    now = [1000.0]
    minted: list[tuple[str, dict[str, str]]] = []
    fail_next = [False]

    def source(_refs: Credentials, repo: str, permissions: dict[str, str]) -> str:
        if fail_next[0]:
            message = "app unavailable"
            raise Invalid(message)
        minted.append((repo, permissions))
        return "fresh-minted-token"

    initial = credentials.StoredToken("expired-launch", 1000.0)
    refresher = gh_refresh.Refresher(
        build_request(), app_references(), initial, clock=lambda: now[0], source=source
    )
    with refresher.serve(gh_dir):
        now[0] = 1060.0
        assert "password=expired-launch\n" in fill(clone, env).stdout
        assert minted == []

        now[0] = 1000.0 + 2 * HOUR
        refreshed = fill(clone, env)
        assert refreshed.returncode == 0, refreshed.stderr
        assert "password=fresh-minted-token\n" in refreshed.stdout
        assert minted == [("tbhb-dev/example-repo", PUSH)]
        assert hosts_token(gh_dir) == "fresh-minted-token"

        now[0] = 1000.0 + 2 * HOUR + 60.0
        assert "password=fresh-minted-token\n" in fill(clone, env).stdout
        assert len(minted) == 1

        fail_next[0] = True
        now[0] = 1000.0 + 4 * HOUR
        refused = fill(clone, env)
        assert refused.returncode != 0
        assert credentials.FRESH_REFUSAL in refused.stderr
    assert not (gh_dir / gh_refresh.SOCKET_NAME).exists()


def test_pr_refresh_symlink_safety_and_proactive_update(tmp_path: Path) -> None:
    gh_dir = runfiles.private_dir(tmp_path / "gh")
    outside = runfiles.private_dir(tmp_path / "outside")
    runfiles.atomic(
        gh_dir / "hosts.yml", credentials.hosts_yaml("github.com", "expired-launch")
    )
    gh_refresh.refresh_if_served({"GH_CONFIG_DIR": str(gh_dir)})
    gh_refresh.refresh_if_served({})
    now = [1000.0]
    initial = credentials.StoredToken("expired-launch", 1000.0)
    refresher = gh_refresh.Refresher(
        build_request(),
        app_references(),
        initial,
        clock=lambda: now[0],
        source=lambda *_a: "pinned-fresh",
    )
    with refresher.serve(gh_dir):
        moved = tmp_path / "moved"
        gh_dir.rename(moved)
        gh_dir.symlink_to(outside, target_is_directory=True)
        now[0] = 1000.0 + 2 * HOUR
        gh_refresh.refresh_if_served({"GH_CONFIG_DIR": str(moved)})
    assert not (outside / "hosts.yml").exists()
    assert hosts_token(moved) == "pinned-fresh"

    proactive_dir = runfiles.private_dir(tmp_path / "proactive")
    runfiles.atomic(
        proactive_dir / "hosts.yml",
        credentials.hosts_yaml("github.com", "expired-launch"),
    )
    proactive = gh_refresh.Refresher(
        build_request(),
        app_references(),
        initial,
        clock=lambda: 1000.0 + 2 * HOUR,
        source=lambda *_a: "proactive-token",
    )
    with proactive.serve(proactive_dir):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if hosts_token(proactive_dir) == "proactive-token":
                break
            time.sleep(0.01)
    assert hosts_token(proactive_dir) == "proactive-token"
