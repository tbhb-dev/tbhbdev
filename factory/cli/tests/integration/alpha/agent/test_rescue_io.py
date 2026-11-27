"""Exercise fault publication with real Git and Gitleaks."""

import hashlib
import os
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
lazy from pathlib import Path

import pytest
from agent_host_example import commit, git, repository
from test_alpha_agent_run_host import make_run, preparation

from tbhbdev.alpha.agent import (
    credentials,
    host_tools,
    process_group,
    push,
    rescue,
    run_host,
)

BRANCH = "agent/example"
REF = f"refs/heads/{BRANCH}"
START_REF = "refs/heads/topic"


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setattr(run_host.admission, "ROOT", tmp_path / "admission")
    workspace, _ = repository(tmp_path)
    origin = tmp_path / "origin"
    (workspace / ".gitleaks.toml").write_text("")
    commit(workspace, "configure scan")
    git(workspace, "push", "origin", "main", "main:refs/heads/topic")
    git(workspace, "switch", "-c", BRANCH)
    original = push.remote_git

    def local(path: Path, *args: str, token: str | None = None) -> str:
        assert path != workspace
        args = tuple(
            str(origin) if part.startswith("https://") else part for part in args
        )
        return original(path, *args, token=token)

    monkeypatch.setattr(push, "remote_git", local)
    monkeypatch.setattr(credentials, "github_token", Mock(return_value="example-token"))
    monkeypatch.setattr(host_tools, "search", Mock(return_value=os.environ["PATH"]))
    return workspace, origin


@pytest.mark.parametrize(
    ("change", "result", "rc"),
    [
        ("committed", "published", 124),
        ("dirty", "published", 124),
        ("secret", "kept local: secret scan found a secret", 124),
        ("vendored secret", "kept local: secret scan found a secret", 7),
        ("earlier secret", "kept local: secret scan found a secret", 7),
        ("unavailable", "kept local: secret scan unavailable", 7),
        ("silent error", "kept local: secret scan unavailable", 7),
        ("scan timeout", "kept local: secret scan unavailable", 7),
        ("scan error", "kept local: secret scan unavailable", 7),
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
    checkout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    result: str,
    rc: int,
) -> None:
    workspace, origin = checkout
    if change == "vendored secret":
        install_vendored_policy(workspace)
    base = git(workspace, "rev-parse", "HEAD")
    run = make_run(
        workspace.parent, timeout=1, start_ref=START_REF, fetch_refs=[START_REF]
    )
    prepared = preparation(run)
    prepared = replace(prepared, paths=replace(prepared.paths, worktree=str(workspace)))
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

    markers = git(origin, "rev-parse", "refs/heads/main", START_REF)
    assert markers.splitlines() == [base, base]
    local = git(workspace, "rev-parse", REF)
    remote = git(origin, "for-each-ref", "--format=%(objectname)", REF)
    expected_tip = local if result == "published" or change == "already pushed" else ""
    if change == "diverged":
        expected_tip = git(workspace, "rev-parse", "other")
    assert remote == expected_tip
    subject = git(workspace, "log", "-1", "--format=%s", REF)
    if change in {"dirty", "secret", "vendored secret", "unavailable", "scan timeout"}:
        assert subject.startswith("rescue:")
        assert git(workspace, "rev-list", "--count", f"{base}..{REF}") == "1"
        assert git(workspace, "status", "--porcelain") == ""
        changed = git(workspace, "show", "--format=", "--name-only", REF)
        assert "tracked" in changed.splitlines()
        assert git(workspace, "show", f"{REF}:notes")
    elif change not in {"scan error", "silent error"}:
        assert not subject.startswith("rescue:")
    if change == "switched":
        assert git(workspace, "log", "-1", "--format=%s").startswith("rescue:")
        assert git(workspace, "log", "-1", "--format=%s", "other") == "other work"
    if change == "clean exit":
        assert git(workspace, "status", "--porcelain") == " M tracked\n?? notes".strip()


def install_vendored_policy(workspace: Path) -> None:
    rules = workspace / "lib/rules/lint/gitleaks/base.toml"
    rules.parent.mkdir(parents=True)
    rules.write_text('[[rules]]\nid = "example-rule"\nregex = "example-test-value"\n')
    commit(workspace, "configure vendored scanner")
    git(workspace, "push", "origin", "HEAD:refs/heads/main", "HEAD:refs/heads/topic")


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
        if change == "vendored secret":
            note = "example-test-value"
        (workspace / "notes").write_text(note if "secret" in change else "notes\n")
    if change in (
        "committed",
        "earlier secret",
        "already pushed",
        "empty commit",
        "diverged",
    ):
        commit(workspace, "build work")
    if change == "earlier secret":
        (workspace / "notes").unlink()
        commit(workspace, "remove test value")
    if change == "already pushed":
        git(workspace, "push", "origin", REF)
    break_checkout(workspace, change)


def break_checkout(workspace: Path, change: str) -> None:
    match change:
        case "checkout error":
            (workspace / ".git" / "index.lock").touch()
        case "symlink index":
            index = workspace / ".git" / "index"
            index.symlink_to(index.rename(index.with_name("original-index")))
        case _:
            return


def scan_fault(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    faults = {
        "unavailable": Mock(side_effect=FileNotFoundError),
        "scan timeout": Mock(side_effect=subprocess.TimeoutExpired("gitleaks", 60)),
        "silent error": Mock(
            return_value=subprocess.CompletedProcess([], 0, "", "0 commits scanned")
        ),
        "scan error": Mock(
            return_value=subprocess.CompletedProcess([], 0, "", "ERR scan failed")
        ),
    }
    if scanner := faults.get(change):
        monkeypatch.setattr(rescue, "subprocess", SimpleNamespace(run=scanner))
