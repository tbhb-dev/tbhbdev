"""Exercise fault publication with real Git and Gitleaks."""

import json
import os
from dataclasses import replace
from functools import partial
from unittest.mock import Mock
lazy from pathlib import Path

import httpx2
import pytest
from agent_host_example import (
    commit,
    constant,
    git,
    host_settings,
    make_run,
    repository,
    run_request,
)
from fake_tool import PRIVATE_CONFIG, RULES_BEYOND_ONE
from run_host_fixtures import preparation

from tbhbdev.alpha import pre_push_guard
from tbhbdev.alpha.agent import rescue, runfiles
from tbhbdev.alpha.agent.model import Profile
from tbhbdev.alpha.agent.run_host import completion, draft_pr, state

BRANCH = "agent/example"
REF = f"refs/heads/{BRANCH}"
START_REF = "refs/heads/topic"
REPO = "tbhb-dev/example-repo"
FOUND = "kept local: secret scan found a secret"
UNAVAILABLE = "kept local: secret scan unavailable"
PULLS = f"https://api.github.com/repos/{REPO}/pulls"


@pytest.fixture(autouse=True)
def pulls(monkeypatch: pytest.MonkeyPatch) -> Mock:
    send = Mock(
        return_value=httpx2.Response(200, json=[{"number": 7, "state": "closed"}])
    )
    client = partial(httpx2.Client, transport=httpx2.MockTransport(send))
    monkeypatch.setattr(httpx2, "Client", client)
    return send


@pytest.fixture
def checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> tuple[Path, str, Mock]:
    private = tmp_path / "private.toml"
    monkeypatch.setattr(draft_pr.attribution, "opened_by", constant((None, None)))
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
        if change == "missing base" and "--symref" in args:
            return ""
        return original(path, *args, token=token)

    remote = Mock(wraps=local)
    monkeypatch.setattr(rescue.push, "remote_git", remote)
    monkeypatch.setattr(rescue.credentials, "github_token", Mock(return_value=None))
    monkeypatch.setattr(rescue.host_tools, "search", lambda: os.environ["PATH"])
    monkeypatch.setattr(pre_push_guard, "agent_fingerprint", Mock(return_value=""))
    monkeypatch.setattr(pre_push_guard, "public_keys", Mock(side_effect=OSError))
    errors = {"silent error": "0 commits scanned", "scan error": "ERR scan failed"}
    if stderr := errors.get(change):
        scan = Mock(return_value=Mock(returncode=0, stderr=stderr))
        monkeypatch.setattr(rescue, "subprocess", Mock(run=scan))
    return workspace, base, remote


@pytest.mark.parametrize(
    ("change", "result"),
    [
        ("dirty", "published"),
        ("other domain", "published"),
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
        ("base only", "nothing to publish"),
        ("clean exit", ""),
        ("review", ""),
        ("fix", ""),
    ],
)
def test_fault_publication(
    checkout: tuple[Path, str, Mock], change: str, result: str, pulls: Mock
) -> None:
    workspace, base, remote = checkout
    start_ref = {"protected": "refs/heads/main"}.get(change)
    start_ref = START_REF if change.startswith("start ref") else start_ref
    target = f"example.invalid/{REPO}" if change == "other domain" else REPO
    request = run_request(
        repo=target, start_ref=start_ref, fetch_refs=[start_ref or START_REF]
    )
    profile = {"review": Profile.REVIEW, "fix": "fix"}.get(change)
    if profile is not None:
        request = Mock(wraps=request, profile=profile)
    files = runfiles.RunFiles(workspace.parent / "runs", "example")
    files.write("launched", "\n")
    files.write("base", base)
    if change == "base only":
        files.write("head", base)
    files.uncertain = change == "uncertain"
    run = Mock(state.Run, files=files, request=request, settings=host_settings())
    ready = Mock(spec=state.Prepared, paths=Mock(worktree=str(workspace)))
    change_checkout(workspace, change)
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
    skipped = ("nothing", "base only", "other domain")
    expected = result in ("published", "nothing to publish") and change not in skipped
    assert [call.args[0].method for call in pulls.call_args_list] == ["GET"] * expected
    if change == "start ref":
        assert pulls.call_args.args[0].url.params["head"] == "tbhb-dev:topic"
    if change in ("dirty", "switched"):
        assert git(workspace, "log", "-1", "--format=%ae") == "run-launcher"


def change_checkout(workspace: Path, change: str) -> None:
    if change in ("nothing", "base only"):
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


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ("dirty with commit", ""),
        ("already pushed", ""),
        ("without opener", ""),
        ("get denied", f"cannot check {BRANCH}"),
        ("get offline", f"cannot check {BRANCH}"),
        ("large", "401 lines, limit 400"),
        ("missing base", "cannot verify the published tip and base branch"),
        ("post denied", "draft attempt failed"),
        ("invalid number", "PR creation returned no number"),
        ("title finding", "gitleaks finds a secret"),
    ],
)
def test_fault_draft(
    checkout: tuple[Path, str, Mock],
    change: str,
    refusal: str,
    pulls: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, base, remote = checkout
    rc = 11 if change == "already pushed" else 124
    run = make_run(workspace.parent)
    run.bazel_shutdown = {}
    ready = preparation(run)
    ready = replace(ready, paths=replace(ready.paths, worktree=str(workspace)))
    run.files.write("launched", "\n")
    run.files.write("base", base)
    subject = "host-x" if change == "title finding" else "build work"
    if change == "large":
        (workspace / "large.py").write_text("n = 1\n" * 401)
    tip = commit(workspace, subject)
    if change == "already pushed":
        git(workspace, "push", "origin", REF)
    error = OSError if change == "already pushed" else None
    if change in ("already pushed", "dirty with commit"):
        monkeypatch.setattr(draft_pr.attribution, "opened_by", constant(("root", None)))
        monkeypatch.setattr(draft_pr.opened, "record_opened", Mock(side_effect=error))
    if change == "dirty with commit":
        staged = workspace / ".git/tbhbdev-preflight" / f"{tip}.stages"
        staged.parent.mkdir()
        tree = git(workspace, "rev-parse", "HEAD^{tree}")
        staged.write_text(f"tree {tree}\ngazelle PASS\n")
        second = commit(workspace, "next change")
        (staged.parent / f"{second}.stages").touch()
        (workspace / "tracked").write_text("unfinished\n")
    pulls.configure_mock(side_effect=replies(change))
    tokens = ["rescue", "fresh"]
    monkeypatch.setattr(rescue.credentials, "github_token", Mock(side_effect=tokens))
    assert completion.finish(run, ready, None, rc) == rc
    assert run.files.path("rc").read_text() == f"{rc}\n"
    assert remote.call_args_list[0].kwargs["token"] == tokens[0]
    requests = [call.args[0] for call in pulls.call_args_list]
    assert [request.method for request in requests] == (
        ["GET"]
        if refusal and change not in ("post denied", "invalid number")
        else ["GET", "POST"]
    )
    assert requests[0].url.params["state"] == "all"
    log = run.files.path("log").read_text()
    if refusal:
        assert not run.files.path("pr").exists()
        assert f"draft pull request: not opened: {refusal}" in log
    else:
        post = requests[1]
        assert str(post.url) == PULLS
        assert post.headers["Authorization"] == "Bearer fresh"
        payload = json.loads(post.content)
        assert (payload["title"], payload["head"]) == (subject, BRANCH)
        assert payload["draft"] is True
        assert run.files.path("pr").read_text().strip() == "31"
        assert "draft pull request: opened 31" in log
        if change == "dirty with commit":
            assert "gazelle" in payload["body"]


def replies(change: str) -> list[httpx2.Response | Exception]:
    lookup = httpx2.Response(403 if change == "get denied" else 200, json=[])
    first = httpx2.ConnectError("offline") if change == "get offline" else lookup
    number = 0 if change == "invalid number" else 31
    status = 403 if change == "post denied" else 201
    return [first, httpx2.Response(status, json={"number": number})]
