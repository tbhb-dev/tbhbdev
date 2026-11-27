"""Exercise fault publication with real Git and Gitleaks."""

import os
from unittest.mock import Mock
lazy from pathlib import Path

import pytest
from agent_host_example import commit, git, host_settings, repository, run_request
from fake_tool import PRIVATE_CONFIG, RULES_BEYOND_ONE

from tbhbdev.alpha import pre_push_guard
from tbhbdev.alpha.agent import rescue, run_host, runfiles

BRANCH = "agent/example"
REF = f"refs/heads/{BRANCH}"
START_REF = "refs/heads/topic"
FOUND = "kept local: secret scan found a secret"
UNAVAILABLE = "kept local: secret scan unavailable"


@pytest.fixture
def checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> tuple[Path, str, Mock]:
    private = tmp_path / "private.toml"
    monkeypatch.setattr(rescue.secret_scan, "private_config", lambda: private)
    policy = PRIVATE_CONFIG + RULES_BEYOND_ONE
    if change != "missing private config":
        canary = "test-rule" if change == "no private canary" else "tbhb-private-canary"
        private.write_text(policy.replace("tbhb-private-canary", canary))
    workspace, _ = repository(tmp_path)
    origin = tmp_path / "origin"
    (workspace / ".gitleaks.toml").touch()
    if change == "vendored secret":
        rules = workspace / rescue.VENDORED_RULES
        rules.parent.mkdir(parents=True)
        rules.write_text('[[rules]]\nid = "example-rule"\nregex = "test-value"\n')
    base = commit(workspace, "configure scan")
    start_tip = "main^" if change == "start ref moved" else "main"
    git(workspace, "push", "origin", "main", f"{start_tip}:{START_REF}")
    git(workspace, "switch", "-c", BRANCH)
    original = rescue.push.remote_git

    def local(path: Path, *args: str, token: str | None = None) -> str:
        assert path != workspace
        args = tuple(str(origin) if arg.startswith("https://") else arg for arg in args)
        return original(path, *args, token=token)

    remote = Mock(wraps=local)
    monkeypatch.setattr(rescue.push, "remote_git", remote)
    monkeypatch.setattr(rescue.credentials, "github_token", Mock(return_value=None))
    monkeypatch.setattr(rescue.host_tools, "search", lambda: os.environ["PATH"])
    return workspace, base, remote


@pytest.mark.parametrize(
    ("change", "result"),
    [
        ("dirty", "published"),
        ("unsigned", "kept local: agent commit signature refused"),
        ("start ref", "published"),
        ("start ref moved", "kept local: start ref moved"),
        ("protected", "kept local: refs/heads/main: push to a protected branch"),
        ("vendored secret", FOUND),
        ("earlier private secret", FOUND),
        ("missing private config", UNAVAILABLE),
        ("no private canary", UNAVAILABLE),
        ("silent error", UNAVAILABLE),
        ("scan error", UNAVAILABLE),
        ("checkout error", "kept local: could not save the run checkout"),
        ("symlink index", "kept local: could not save the run checkout"),
        ("start ref rewrite", "kept local: could not publish the run branch"),
        ("switched", "kept local: checkout left the run branch"),
        ("uncertain", "kept local: harness shutdown unconfirmed"),
        ("already pushed", "nothing to publish"),
        ("empty commit", "published"),
        ("nothing", "nothing to publish"),
        ("clean exit", ""),
    ],
)
def test_fault_publication(
    checkout: tuple[Path, str, Mock],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    result: str,
) -> None:
    workspace, base, remote = checkout
    start_ref = START_REF if change.startswith("start ref") else None
    if change == "protected":
        start_ref = "refs/heads/main"
    request = run_request(start_ref=start_ref, fetch_refs=[start_ref or START_REF])
    files = runfiles.RunFiles(workspace.parent / "runs", "example")
    files.write("launched", "\n")
    files.write("base", base)
    files.uncertain = change == "uncertain"
    run = Mock(run_host.Run, files=files, request=request, settings=host_settings())
    ready = Mock(spec=run_host.Prepared, paths=Mock(worktree=str(workspace)))
    change_checkout(workspace, change)
    scan_fault(monkeypatch, change)
    rescue.on_fault(run, ready, 0 if change == "clean exit" else 7)
    log = files.path("log")
    assert log.exists() == bool(result)
    if result:
        assert log.read_text().startswith(f"fault publication: {result}")
    calls = [call.args[1] for call in remote.call_args_list]
    assert ("push" in calls) == (result == "published" or change == "start ref rewrite")
    if result == "published":
        destination = start_ref or REF
        tip = git(workspace, "rev-parse", REF)
        assert remote.call_args.args[-1] == f"{tip}:{destination}"
        assert files.path("branch").read_text() == destination.split("/", 2)[-1]
    if change in ("unsigned", "start ref moved"):
        assert files.path("head").read_text() == git(workspace, "rev-parse", REF)
    if change in ("dirty", "switched"):
        assert git(workspace, "log", "-1", "--format=%ae") == "run-launcher"


def change_checkout(workspace: Path, change: str) -> None:
    if change == "nothing":
        return
    if change == "switched":
        git(workspace, "switch", "-c", "other")
    if change == "start ref rewrite":
        git(workspace, "reset", "--hard", "HEAD^")
        (workspace / ".gitleaks.toml").touch()
    notes = workspace / "notes"
    secrets = {"vendored secret": "test-value", "earlier private secret": "host-x"}
    if change != "empty commit":
        notes.write_text(secrets.get(change, "repo-x"))
    if change == "unsigned":
        env = dict(os.environ, GIT_AUTHOR_EMAIL=pre_push_guard.AGENT_EMAIL)
        commit(workspace, "unsigned work", env=env)
    commits = ("already pushed", "earlier private secret", "empty commit")
    if change.startswith("start ref") or change in commits:
        commit(workspace, "build work")
    if change == "earlier private secret":
        notes.unlink()
        commit(workspace, "remove test value")
    if change == "already pushed":
        git(workspace, "push", "origin", REF)
    if change == "checkout error":
        (workspace / ".git/index.lock").touch()
    if change == "symlink index":
        index = workspace / ".git/index"
        index.symlink_to(index.rename(index.with_name("original-index")))


def scan_fault(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    monkeypatch.setattr(pre_push_guard, "agent_fingerprint", Mock(return_value=""))
    monkeypatch.setattr(pre_push_guard, "public_keys", Mock(side_effect=OSError))
    errors = {"silent error": "0 commits scanned", "scan error": "ERR scan failed"}
    if stderr := errors.get(change):
        scan = Mock(return_value=Mock(returncode=0, stderr=stderr))
        monkeypatch.setattr(rescue, "subprocess", Mock(run=scan))
