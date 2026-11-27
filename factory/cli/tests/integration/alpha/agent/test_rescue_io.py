"""Exercise fault publication with real Git and Gitleaks."""

import hashlib
import shutil
import subprocess
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
lazy from pathlib import Path

import pytest
from agent_host_example import (
    commit,
    git,
    host_settings,
    repository,
    run_request,
)

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
    gitleaks = shutil.which("gitleaks")
    assert gitleaks is not None
    original_resolve = host_tools.resolve

    def resolve(name: str) -> str:
        return gitleaks if name == "gitleaks" else original_resolve(name)

    monkeypatch.setattr(host_tools, "resolve", resolve)
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
        ("missing ref", "kept local: could not save the run checkout", 7),
        ("symlink index", "kept local: could not save the run checkout", 7),
        ("diverged", "kept local: could not publish the run branch", 7),
        ("switched", "kept local: checkout left the run branch", 7),
        ("switched clean", "kept local: checkout left the run branch", 7),
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
    run, prepared = build_run(workspace)
    monkeypatch.setattr(run_host, "preflight", Mock())
    monkeypatch.setattr(process_group, "prepare", Mock(return_value=prepared))

    def harness(*_args: object) -> int:
        run.files.write("launched", "\n")
        change_checkout(workspace, change)
        run.files.uncertain = change == "uncertain"
        wait = (
            Mock(side_effect=subprocess.TimeoutExpired("harness", 1))
            if rc == 124
            else Mock(return_value=rc)
        )
        child = Mock(wait=wait)
        return run_host.wait_harness(run, child)

    monkeypatch.setattr(run_host, "run_harness", harness)
    scan_fault(monkeypatch, change)
    assert run_host.execute(run, "example") == rc
    assert run.files.path("rc").read_text().strip() == str(rc)
    expected = [f"fault publication: {result}"] if result else []
    log = run.files.path("log").read_text().splitlines()
    assert [line for line in log if line.startswith("fault publication:")] == expected

    assert git(origin, "rev-parse", "refs/heads/main") == base
    assert git(origin, "rev-parse", "refs/heads/topic") == base
    local = git(workspace, "rev-parse", REF)
    remote = git(origin, "for-each-ref", "--format=%(objectname)", REF)
    if change == "diverged":
        assert remote == git(workspace, "rev-parse", "other")
    elif result == "published" or change == "already pushed":
        assert remote == local
    else:
        assert remote == ""
    subject = git(workspace, "log", "-1", "--format=%s", REF)
    if change in {"dirty", "secret", "vendored secret", "unavailable", "scan timeout"}:
        assert subject.startswith("rescue:")
        assert git(workspace, "rev-list", "--count", f"{base}..{REF}") == "1"
        assert git(workspace, "status", "--porcelain") == ""
        assert (
            "tracked"
            in git(workspace, "show", "--format=", "--name-only", REF).splitlines()
        )
        assert git(workspace, "show", f"{REF}:notes")
    elif change not in {"scan error", "silent error"}:
        assert not subject.startswith("rescue:")
    if change == "switched":
        assert git(workspace, "log", "-1", "--format=%s").startswith("rescue:")
        assert git(workspace, "log", "-1", "--format=%s", "other") == "other work"
    if change == "clean exit":
        assert git(workspace, "status", "--porcelain") == " M tracked\n?? notes".strip()


@pytest.mark.parametrize("missing", ["prepared", "build", "launched", "base"])
def test_fault_publication_requires_launched_build(
    tmp_path: Path, missing: str
) -> None:
    run, prepared = build_run(tmp_path / "checkout")
    if missing == "build":
        run.request = replace(run.request, profile=run_host.Profile.REVIEW)
    for name in ("launched", "base"):
        if missing != name:
            run.files.write(name, "present")

    rescue.on_fault(run, None if missing == "prepared" else prepared, 7)

    assert not run.files.path("log").exists()


def test_fault_publication_uses_private_git_config(checkout: tuple[Path, Path]) -> None:
    workspace, origin = checkout
    run, _ = build_run(workspace)
    base = git(workspace, "rev-parse", "HEAD")
    run.files.write("base", base)
    config = workspace / ".git" / "config"
    original = config.read_text()
    (workspace / "notes").write_text("notes\n")
    config.write_text("[malformed\n")
    try:
        assert rescue.publish(run, workspace) == "published"
    finally:
        config.write_text(original)

    local = git(workspace, "rev-parse", REF)
    assert git(origin, "rev-parse", REF) == local
    assert git(workspace, "show", f"{local}:notes") == "notes"
    assert git(workspace, "status", "--porcelain") == ""


def build_run(workspace: Path) -> tuple[run_host.Run, run_host.Prepared]:
    root = workspace.parent
    settings = host_settings()
    request = run_request(timeout=1, start_ref=START_REF, fetch_refs=[START_REF])
    files = runfiles.RunFiles(root / "runs", "example")
    paths = run_host.RunPaths(
        str(root), str(workspace), str(root), str(root / "codex"), str(root / "audit")
    )
    delivery = credentials.Delivery({}, None, gh_fallback=False, claude_profile=None)
    prepared = run_host.Prepared(paths, delivery, None)
    return (
        run_host.Run(
            files,
            request,
            settings,
            settings.hosts[0],
            root,
            root,
            0,
            time.monotonic() + 10,
        ),
        prepared,
    )


def install_vendored_policy(workspace: Path) -> None:
    rules = workspace / "lib/rules/lint/gitleaks/base.toml"
    rules.parent.mkdir(parents=True)
    rules.write_text('[[rules]]\nid = "example-rule"\nregex = "example-test-value"\n')
    commit(workspace, "configure vendored scanner")
    git(workspace, "push", "origin", "HEAD:refs/heads/main", "HEAD:refs/heads/topic")


def change_checkout(workspace: Path, change: str) -> None:
    if change == "nothing":
        return
    if change == "switched clean":
        git(workspace, "switch", "-c", "other")
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
    if change == "checkout error":
        (workspace / ".git" / "index.lock").touch()
    if change == "missing ref":
        (workspace / ".git" / "HEAD").write_text("ref: refs/heads/missing\n")
    if change == "symlink index":
        index = workspace / ".git" / "index"
        index.symlink_to(index.rename(index.with_name("original-index")))


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
