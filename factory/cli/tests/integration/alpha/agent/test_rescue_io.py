"""Exercise fault publication with real Git and Gitleaks."""

import os
import subprocess
from unittest.mock import Mock
lazy from pathlib import Path

import pytest
from agent_host_example import commit, git, host_settings, repository, run_request
from fake_tool import PRIVATE_CONFIG, RULES_BEYOND_ONE

from tbhbdev.alpha import pre_push_guard
from tbhbdev.alpha.agent import rescue, runfiles
from tbhbdev.alpha.agent.run_host import state

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
    msg = rescue.credentials.FRESH_REFUSAL
    tok = (
        Mock(side_effect=rescue.credentials.Invalid(msg))
        if change == "credential refused"
        else Mock(return_value="fresh-token")
    )
    monkeypatch.setattr(rescue.credentials, "fresh_token", tok)
    monkeypatch.setattr(rescue.host_tools, "search", lambda: os.environ["PATH"])
    monkeypatch.setattr(pre_push_guard, "agent_fingerprint", Mock(return_value=""))
    monkeypatch.setattr(pre_push_guard, "public_keys", Mock(side_effect=OSError))
    errors = {"silent error": "0 commits scanned", "scan error": "ERR scan failed"}
    if change in errors or change == "finding without commit":
        code = rescue.FINDING if change == "finding without commit" else 0
        scan = Mock(
            return_value=Mock(returncode=code, stderr=errors.get(change, ""), stdout="")
        )
        monkeypatch.setattr(rescue, "subprocess", Mock(run=scan))
    return workspace, base, remote


@pytest.mark.parametrize(
    ("change", "result"),
    [
        ("dirty", "published"),
        ("credential refused", f"kept local: {rescue.credentials.FRESH_REFUSAL}"),
        (
            "unsigned",
            f"kept local: {REF}: agent commit {{sha}} lacks a valid agent signature",
        ),
        ("start ref", "published"),
        ("start ref moved", "kept local: start ref moved"),
        ("protected", "kept local: refs/heads/main: push to a protected branch"),
        ("vendored secret", FOUND),
        ("earlier private secret", FOUND),
        ("finding without commit", FOUND),
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
    checkout: tuple[Path, str, Mock], change: str, result: str
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
    run = Mock(state.Run, files=files, request=request, settings=host_settings())
    ready = Mock(spec=state.Prepared, paths=Mock(worktree=str(workspace)))
    change_checkout(workspace, change)
    if change == "unsigned":
        result = result.format(sha=git(workspace, "rev-parse", REF)[:7])
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
        assert remote.call_args.kwargs == {"token": "fresh-token"}
        assert files.path("branch").read_text() == destination.split("/", 2)[-1]
    if change in ("unsigned", "start ref moved"):
        assert files.path("head").read_text() == git(workspace, "rev-parse", REF)
    if change in ("dirty", "switched"):
        assert git(workspace, "log", "-1", "--format=%ae") == "run-launcher"


@pytest.fixture
def agent_key(tmp_path: Path) -> Path:
    key = tmp_path / "agent-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


@pytest.mark.parametrize(
    "change", ["signed", "unsigned after rebase", "run secret", "main secret", "topic"]
)
def test_publication_after_main_advances(
    checkout: tuple[Path, str, Mock],
    agent_key: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    workspace, base, remote = checkout
    fingerprint = pre_push_guard.key_fingerprint(str(agent_key.with_suffix(".pub")))
    assert fingerprint
    monkeypatch.setattr(pre_push_guard, "agent_fingerprint", lambda: fingerprint)
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Example",
        GIT_AUTHOR_EMAIL=pre_push_guard.AGENT_EMAIL,
        GIT_COMMITTER_NAME="Example",
        GIT_COMMITTER_EMAIL="example@example.com",
    )
    start_ref = START_REF if change == "topic" else None
    if start_ref:
        (workspace / "topic").write_text("topic work")
        base = commit(workspace, "existing topic work", env=env)
        git(workspace, "push", "origin", f"HEAD:{start_ref}")

    upstream = workspace.parent / "clone"
    git(upstream, "pull", "--ff-only")
    (upstream / "upstream").write_text("host-x" if change == "main secret" else "main")
    if change == "run secret":
        (upstream / ".gitleaks.toml").unlink()
    main = commit(upstream, "advance main", env=env)
    git(upstream, "push", "origin", "main")

    work = workspace / "work"
    work.write_text("host-x" if change == "run secret" else "build work")
    commit(workspace, "build work", env=env)
    if change == "run secret":
        work.unlink()
        commit(workspace, "remove test value", env=env)
    origin = workspace.parent / "origin"
    git(workspace, "fetch", str(origin), "HEAD")
    assert git(workspace, "rev-parse", "FETCH_HEAD") == main
    assert git(workspace, "rev-parse", "refs/remotes/origin/main") != main
    signing = ("-c", "gpg.format=ssh", "-c", f"user.signingkey={agent_key}")
    sign = "--no-gpg-sign" if change == "unsigned after rebase" else "-S"
    if start_ref:
        git(workspace, *signing, "commit", "--amend", "--no-edit", sign, env=env)
    operation = ("merge", "--no-edit") if start_ref else ("rebase",)
    git(workspace, *signing, *operation, sign, main, env=env)
    tip = git(workspace, "rev-parse", "HEAD")
    files = runfiles.RunFiles(workspace.parent / "runs", "example")
    files.write("base", base)
    request = run_request(start_ref=start_ref, fetch_refs=[START_REF])
    run = Mock(state.Run, files=files, request=request, settings=host_settings())

    result = rescue.publish(run, workspace)

    if change == "unsigned after rebase":
        assert f"agent commit {tip[:7]} lacks a valid agent signature" in result
    elif change == "run secret":
        secret = git(workspace, "rev-parse", "HEAD^")
        assert result == f"{FOUND} in commit {secret[:7]}"
    else:
        assert result == "published"
        assert git(origin, "rev-parse", start_ref or REF) == tip
    assert any(call.args[1] == "push" for call in remote.call_args_list) == (
        result == "published"
    )


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
