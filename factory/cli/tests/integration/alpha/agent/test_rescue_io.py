"""Exercise fault publication with real Git and Gitleaks."""

import hashlib
import os
import subprocess
from math import inf
from unittest.mock import Mock
lazy from pathlib import Path

import pytest
from agent_host_example import commit, git, host_settings, repository, run_request
from fake_tool import PRIVATE_CONFIG, RULES_BEYOND_ONE

from tbhbdev.alpha.agent import (
    credentials,
    host_tools,
    process_group,
    push,
    rescue,
    run_host,
    runfiles,
)

BRANCH = "agent/example"
REF = f"refs/heads/{BRANCH}"
START_REF = "refs/heads/topic"
FOUND = "kept local: secret scan found a secret"
UNAVAILABLE = "kept local: secret scan unavailable"


@pytest.fixture
def checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> tuple[Path, Path, str]:
    monkeypatch.setattr(run_host.admission, "ROOT", tmp_path / "admission")
    private = tmp_path / "private.toml"
    monkeypatch.setattr(rescue.secret_scan, "private_config", lambda: private)
    policy = PRIVATE_CONFIG + RULES_BEYOND_ONE
    if change == "no private canary":
        policy = policy.replace("tbhb-private-canary", "test-rule")
    if change != "missing private config":
        private.write_text(policy)
    workspace, _ = repository(tmp_path)
    origin = tmp_path / "origin"
    (workspace / ".gitleaks.toml").touch()
    if change == "vendored secret":
        rules = workspace / rescue.VENDORED_RULES
        rules.parent.mkdir(parents=True)
        rules.write_text('[[rules]]\nid = "example-rule"\nregex = "test-value"\n')
    base = commit(workspace, "configure scan")
    git(workspace, "push", "origin", "main", f"main:{START_REF}")
    git(workspace, "switch", "-c", BRANCH)
    original = push.remote_git

    def local(path: Path, *args: str, token: str | None = None) -> str:
        assert path != workspace
        mapped = [str(origin) if arg.startswith("https://") else arg for arg in args]
        return original(path, *mapped, token=token)

    monkeypatch.setattr(push, "remote_git", local)
    monkeypatch.setattr(credentials, "github_token", Mock(return_value="example-token"))
    monkeypatch.setattr(host_tools, "search", Mock(return_value=os.environ["PATH"]))
    return workspace, origin, base


@pytest.mark.parametrize(
    ("change", "result", "rc"),
    [
        ("committed", "published", 124),
        ("dirty", "published", 124),
        ("secret", FOUND, 124),
        ("vendored secret", FOUND, 7),
        ("committed earlier secret", FOUND, 7),
        ("committed private", FOUND, 7),
        ("missing private config", UNAVAILABLE, 7),
        ("no private canary", UNAVAILABLE, 7),
        ("silent error", UNAVAILABLE, 7),
        ("scan timeout", UNAVAILABLE, 7),
        ("scan error", UNAVAILABLE, 7),
        ("checkout error", "kept local: could not save the run checkout", 7),
        ("symlink index", "kept local: could not save the run checkout", 7),
        ("diverged", "kept local: could not publish the run branch", 7),
        ("switched", "kept local: checkout left the run branch", 7),
        ("uncertain", "kept local: harness shutdown unconfirmed", 7),
        ("already pushed", "nothing to publish", 7),
        ("empty commit", "published", 7),
        ("nothing", "nothing to publish", 7),
        ("clean exit", "", 0),
    ],
)
def test_fault_publication(
    checkout: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    result: str,
    rc: int,
) -> None:
    workspace, origin, base = checkout
    run, prepared = build_run(workspace)
    monkeypatch.setattr(run_host, "preflight", Mock())
    monkeypatch.setattr(process_group, "prepare", Mock(return_value=prepared))

    def harness(*_args: object) -> int:
        run.files.write("launched", "\n")
        change_checkout(workspace, change)
        run.files.uncertain = change == "uncertain"
        expired = subprocess.TimeoutExpired("harness", 1) if rc == 124 else None
        wait = Mock(return_value=rc, side_effect=expired)
        return run_host.wait_harness(run, Mock(wait=wait))

    monkeypatch.setattr(run_host, "run_harness", harness)
    scan_fault(monkeypatch, change)
    assert run_host.execute(run, "example") == rc
    assert run.files.path("rc").read_text().strip() == str(rc)
    expected = [f"fault publication: {result}"] if result else []
    log = run.files.path("log").read_text().splitlines()
    assert [line for line in log if line.startswith("fault publication:")] == expected

    assert git(origin, "rev-parse", "refs/heads/main", START_REF) == f"{base}\n{base}"
    pushed = {"published": REF, "already pushed": REF, "diverged": "other"}
    expected_ref = pushed.get(result) or pushed.get(change)
    expected = git(workspace, "rev-parse", expected_ref) if expected_ref else ""
    assert git(origin, "for-each-ref", "--format=%(objectname)", REF) == expected
    subject = git(workspace, "log", "-1", "--format=%s", REF)
    saved = change in {"dirty", "secret", "vendored secret"} or result == UNAVAILABLE
    assert subject.startswith("rescue:") == saved
    if saved:
        assert git(workspace, "rev-list", "--count", f"{base}..{REF}") == "1"
        assert git(workspace, "status", "--porcelain") == ""
        assert git(workspace, "show", f"{REF}:tracked") == "changed"
        assert git(workspace, "show", f"{REF}:notes")
    if change == "switched":
        assert git(workspace, "log", "-1", "--format=%s").startswith("rescue:")
        assert git(workspace, "log", "-1", "--format=%s", "other") == "other work"
    if change == "clean exit":
        assert git(workspace, "status", "--porcelain") == " M tracked\n?? notes".strip()


def build_run(workspace: Path) -> tuple[run_host.Run, run_host.Prepared]:
    root = workspace.parent
    settings = host_settings()
    request = run_request(timeout=1, start_ref=START_REF, fetch_refs=[START_REF])
    files = runfiles.RunFiles(root / "runs", "example")
    run = run_host.Run(files, request, settings, settings.hosts[0], root, root, 0, inf)
    home = str(root)
    paths = run_host.RunPaths(
        home, str(workspace), home, f"{home}/codex", f"{home}/audit"
    )
    delivery = credentials.Delivery({}, None, gh_fallback=False, claude_profile=None)
    return run, run_host.Prepared(paths, delivery, None)


def change_checkout(workspace: Path, change: str) -> None:
    if change == "nothing":
        return
    if change in ("diverged", "switched"):
        git(workspace, "switch", "-c", "other")
        commit(workspace, "other work")
        if change == "diverged":
            git(workspace, "push", "origin", f"other:{REF}")
            git(workspace, "switch", BRANCH)
    if change != "empty commit":
        (workspace / "tracked").write_text("changed\n")
        note = "ghp_" + hashlib.sha256(b"fixture").hexdigest()[:36]
        notes = {"vendored secret": "test-value", "committed private": "host-x"}
        fallback = note if "secret" in change else "repo-x\n"
        (workspace / "notes").write_text(notes.get(change, fallback))
    commit_changes = {"already pushed", "empty commit", "diverged"}
    if change.startswith("committed") or change in commit_changes:
        commit(workspace, "build work")
    if change == "committed earlier secret":
        (workspace / "notes").unlink()
        commit(workspace, "remove test value")
    if change == "already pushed":
        git(workspace, "push", "origin", REF)
    if change == "checkout error":
        (workspace / ".git/index.lock").touch()
    if change == "symlink index":
        index = workspace / ".git/index"
        index.symlink_to(index.rename(index.with_name("original-index")))


def scan_fault(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    errors = {"silent error": "0 commits scanned", "scan error": "ERR scan failed"}
    if change == "scan timeout":
        scanner = Mock(side_effect=subprocess.TimeoutExpired("gitleaks", 60))
    elif stderr := errors.get(change):
        scanner = Mock(return_value=subprocess.CompletedProcess([], 0, "", stderr))
    else:
        return
    monkeypatch.setattr(rescue, "subprocess", Mock(run=scanner))
