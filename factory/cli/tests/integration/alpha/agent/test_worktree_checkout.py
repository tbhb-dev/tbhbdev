"""Checkout orchestration for fresh run workspaces."""

from __future__ import annotations

import os
import subprocess
import sys
from functools import partial
from pathlib import Path
from unittest.mock import Mock
lazy from types import ModuleType

import pytest
from agent_host_example import (
    commit,
    constant,
    git,
    preparation_failure,
    pull_requests_app_settings,
    repository,
    run_request,
)
from fake_tool import pythonpath, write_fake

from tbhbdev import secrets
from tbhbdev.alpha import gh_app_api, pre_push_guard, run_stop_hook
from tbhbdev.alpha.agent import (
    credentials,
    gh_refresh,
    push,
    runfiles,
    worktree_checkout,
    worktree_clone,
    worktree_git,
    worktree_hooks,
    worktree_mirror,
)
from tbhbdev.alpha.agent.model import Invalid

URL = "https://github.com/tbhb-dev/example-repo.git"


def test_workspace_fetches_from_the_mirror_and_pushes_to_github(
    tmp_path: Path,
) -> None:
    seed, clone = repository(tmp_path)
    path = worktree_checkout.create(clone, "build-1", run_request())
    mirror = worktree_mirror.mirror_path(tmp_path, run_request().repo)
    assert git(path, "config", "remote.origin.url") == str(mirror)
    assert git(path, "config", "remote.origin.pushurl") == (
        "https://github.com/tbhb-dev/example-repo.git"
    )
    (seed / "tracked").write_text("later\n")
    head = commit(seed, "later")
    git(seed, "push", "origin", "main")
    worktree_git.git(mirror, "fetch", "origin", "+refs/heads/main:refs/heads/main")
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}
    git(path, "fetch", "origin", env=env)
    assert git(path, "rev-parse", "origin/main") == head


def test_build_fetches_default_and_creates_one_branch(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    (seed / "tracked").write_text("new remote content\n")
    head = commit(seed, "remote change")
    git(seed, "push", "origin", "main")
    path = worktree_checkout.create(clone, "build-1", run_request())
    assert path == clone / ".worktrees" / "agent-build-1"
    assert git(path, "branch", "--show-current") == "agent/build-1"
    assert git(path, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "HEAD") != head
    assert (path / "tracked").read_text() == "new remote content\n"
    mirror = worktree_mirror.mirror_path(tmp_path, run_request().repo)
    assert (path / ".git").is_dir()
    assert (mirror / "agent-mirror.lock").exists()
    assert not (path / ".git" / "objects" / "info" / "alternates").exists()
    assert git(path, "remote", "get-url", "--push", "origin") == (
        "https://github.com/tbhb-dev/example-repo.git"
    )
    with pytest.raises(Invalid, match="run preparation failed at clone"):
        worktree_checkout.create(clone, "build-1", run_request())
    assert git(clone, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.parametrize("profile", ["build", "review"])
def test_requested_refs_reach_clone_without_changing_checkout(
    tmp_path: Path, profile: str
) -> None:
    seed, clone = repository(tmp_path)
    default = git(seed, "rev-parse", "HEAD")
    (seed / "tracked").write_text("PR change\n")
    head = commit(seed, "PR head")
    git(seed, "push", "origin", "HEAD:refs/pull/391/head", "HEAD:refs/heads/fix-392")
    request = run_request(
        profile=profile,
        commit=head if profile == "review" else None,
        fetch_refs=["refs/pull/391/head", "refs/heads/fix-392", "refs/heads/main"],
    )
    path = worktree_checkout.create(clone, "refs", request)
    assert git(path, "rev-parse", "origin/fetched/pull/391/head") == head
    assert git(path, "rev-parse", "origin/fetched/heads/fix-392") == head
    assert git(path, "rev-parse", "origin/fetched/heads/main") == default
    assert git(path, "rev-parse", "origin/main") == default
    assert git(path, "rev-parse", "HEAD") == (head if profile == "review" else default)
    assert git(path, "branch", "--show-current") == (
        "" if profile == "review" else "agent/refs"
    )
    mirror = worktree_mirror.mirror_path(tmp_path, request.repo)
    assert git(mirror, "rev-parse", "refs/heads/main") == default
    assert not (mirror / "FETCH_HEAD").exists()


def test_build_starts_at_the_fetched_branch(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    default = git(seed, "rev-parse", "HEAD")
    branch_tip = commit(seed, "PR head")
    ref = "refs/heads/fix/topic"
    git(seed, "push", "origin", f"HEAD:{ref}")
    run = run_request(start_ref=ref, fetch_refs=[ref])

    path = worktree_checkout.create(clone, "fix", run)

    assert git(path, "rev-parse", "HEAD", "origin/fetched/heads/fix/topic") == (
        f"{branch_tip}\n{branch_tip}"
    )
    assert git(path, "branch", "--show-current") == "agent/fix"
    assert git(path, "rev-parse", "origin/main") == default


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("refs/heads/main", "cannot name the default branch"),
        ("refs/heads/topic.", "is not a valid Git ref"),
    ],
)
def test_build_refuses_default_and_malformed_start_refs(
    tmp_path: Path, ref: str, message: str
) -> None:
    _, clone = repository(tmp_path)
    with pytest.raises(Invalid, match=f"--start-ref {message}"):
        worktree_checkout.create(
            clone, "invalid", run_request(start_ref=ref, fetch_refs=[ref])
        )
    assert not (clone / ".worktrees" / "agent-invalid").exists()


@pytest.mark.parametrize("ref", ["refs/pull/392/head", "refs/heads/missing"])
def test_missing_requested_ref_fails_preparation_with_its_name(
    tmp_path: Path, ref: str
) -> None:
    _, clone = repository(tmp_path)
    request = run_request(fetch_refs=[ref, "refs/heads/main"])
    with pytest.raises(Invalid) as caught:
        worktree_checkout.create(clone, "missing", request)
    assert str(caught.value).startswith(
        "run preparation failed at mirror fetch "
        "(https://github.com/tbhb-dev/example-repo.git); requested refs: "
        f"{ref}, refs/heads/main"
    )
    assert not (clone / ".worktrees" / "agent-missing").exists()


@pytest.mark.parametrize("dirty", [False, True])
def test_clone_work_is_preserved(tmp_path: Path, dirty: bool) -> None:
    seed, clone = repository(tmp_path)
    old = git(clone, "rev-parse", "HEAD")
    if dirty:
        (clone / "tracked").write_text("local edits\n")
    else:
        git(clone, "checkout", "-b", "existing")
    (seed / "tracked").write_text("remote edits\n")
    head = commit(seed, "remote change")
    git(seed, "push", "origin", "main")
    path = worktree_checkout.create(clone, "build-1", run_request())
    assert git(path, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "HEAD") == old
    assert (clone / "tracked").read_text() == (
        "local edits\n" if dirty else "initial\n"
    )


def test_review_detaches_at_resolved_commit(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    git(seed, "checkout", "-b", "topic")
    head = commit(seed, "review")
    git(seed, "push", "origin", "topic")
    path = worktree_checkout.create(
        clone,
        "review-1",
        run_request(profile="review", commit=head),
    )
    assert path == clone / ".worktrees" / "agent-review-1"
    assert git(path, "rev-parse", "HEAD") == head
    assert git(path, "branch", "--show-current") == ""


def test_missing_commit_is_refused(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    with pytest.raises(Invalid, match="run preparation failed at mirror fetch"):
        worktree_checkout.create(
            clone,
            "missing",
            run_request(profile="review", commit="0" * 40),
        )
    assert not (clone / ".worktrees" / "agent-missing").exists()


def test_missing_clone_directory_is_created(tmp_path: Path) -> None:
    repository(tmp_path)
    clone = tmp_path / "missing" / "example-repo"
    request = run_request()
    workspace = worktree_checkout.create(clone, "example", request)
    assert workspace == clone / ".worktrees" / "agent-example"
    assert (workspace / ".git").is_dir()


def test_symbolic_link_clone_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    clone = tmp_path / "link"
    clone.symlink_to(real)
    with pytest.raises(Invalid, match="repo clone paths must not be symbolic links"):
        worktree_checkout.create(clone, "example", run_request())
    assert not (real / ".worktrees").exists()


def test_unwritable_clone_parent_is_refused(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(Invalid, match="run preparation failed at repo clone directory"):
        worktree_checkout.create(blocker / "repo", "example", run_request())


@pytest.mark.parametrize("profile", ["build", "review"])
def test_worktree_git_contract_under_one_stable_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    seed, clone = repository(tmp_path)
    commit_id = git(seed, "rev-parse", "HEAD")
    calls: list[tuple[Path, tuple[str, ...]]] = []
    original = worktree_git.git

    def checked(path: Path, *args: str) -> str:
        assert path != clone
        calls.append((path, args))
        return original(path, *args)

    monkeypatch.setattr(worktree_git, "git", checked)
    request = run_request(
        profile=profile, commit=commit_id if profile == "review" else None
    )
    path = worktree_checkout.create(clone, "example", request)
    mirror = worktree_mirror.mirror_path(tmp_path, request.repo)
    assert all(directory in (mirror, path) for directory, _args in calls)
    assert (mirror / "agent-mirror.lock").is_file()
    assert (mirror.stat().st_mode & 0o777) == 0o700
    assert git(path, "rev-parse", "HEAD") == commit_id
    assert git(path, "config", "remote.origin.url") == str(mirror)
    assert (
        git(path, "config", "remote.origin.pushurl")
        == "https://github.com/tbhb-dev/example-repo.git"
    )
    assert (path / ".git/hooks/prepare-commit-msg").is_file()


def test_host_git_never_runs_hooks_or_monitor(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    marker = tmp_path / "executed"
    script = f'#!/bin/sh\ntouch "{marker}"\n'
    for name in ("post-checkout", "monitor"):
        path = clone / ".git" / "hooks" / name
        path.write_text(script)
        path.chmod(0o700)
    git(clone, "config", "core.fsmonitor", str(clone / ".git" / "hooks" / "monitor"))
    (seed / "tracked").write_text("remote update\n")
    commit(seed, "remote update")
    git(seed, "push", "origin", "main")
    worktree_checkout.create(clone, "safe", run_request())
    assert not marker.exists()


def test_existing_clone_symlink_is_refused_without_following_it(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    target = tmp_path / "untouched"
    target.mkdir()
    parent = clone / ".worktrees"
    parent.mkdir()
    (parent / "agent-example").symlink_to(target, target_is_directory=True)
    with pytest.raises(Invalid, match="run preparation failed at clone"):
        worktree_checkout.create(clone, "example", run_request())
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    "plant", ["core.fsmonitor", "core.pager", "filter.planted.smudge"]
)
def test_host_git_never_returns_to_a_launched_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: str
) -> None:
    _, clone = repository(tmp_path)
    launched: set[Path] = set()
    calls: list[Path] = []
    original = worktree_git.git

    def checked(path: Path, *args: str) -> str:
        assert path not in launched
        assert path != clone
        calls.append(path)
        return original(path, *args)

    monkeypatch.setattr(worktree_git, "git", checked)
    first = worktree_checkout.create(clone, "first", run_request())
    launched.add(first)
    marker = tmp_path / "fetch-marker"
    marker.write_text("untouched\n")
    (first / ".git" / "FETCH_HEAD").symlink_to(marker)
    executed = tmp_path / "executed"
    script = tmp_path / "plant"
    script.write_text(f'#!/bin/sh\nprintf invoked > "{executed}"\n')
    script.chmod(0o700)
    git(first, "config", plant, str(script))
    second = worktree_checkout.create(clone, "second", run_request())
    assert marker.read_text() == "untouched\n"
    assert not executed.exists()
    mirror = worktree_mirror.mirror_path(tmp_path, run_request().repo)
    assert set(calls) == {mirror, first, second}


@pytest.mark.parametrize("profile", ["build", "review"])
@pytest.mark.parametrize("xdg", [False, True, "alias", "parent-alias"])
def test_prelaunch_git_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, xdg: bool | str
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if not xdg:
        monkeypatch.delenv("XDG_STATE_HOME")
    if xdg == "alias":
        target = tmp_path / "state-target"
        target.mkdir()
        alias = tmp_path / "state-alias"
        alias.symlink_to(target, target_is_directory=True)
        monkeypatch.setenv("XDG_STATE_HOME", str(alias))
    if xdg == "parent-alias":
        state = tmp_path / "state"
        state.mkdir()
        target = tmp_path / "parent-target"
        target.mkdir()
        (state / "tbhbdev").symlink_to(target, target_is_directory=True)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".worktrees").mkdir()
    full_sha = "c" * 40
    request = run_request(
        profile=profile, commit=full_sha if profile == "review" else None
    )
    mirror = worktree_mirror.mirror_path(tmp_path, request.repo)
    workspace = clone / ".worktrees" / "agent-protocol"
    calls: list[tuple[Path, tuple[str, ...]]] = []

    responses = {"ls-remote": "ref: refs/heads/main\tHEAD", "rev-parse": full_sha}

    def checked(path: Path, *args: str) -> str:
        # The mirror tests assert the fetch flags. This test checks when it runs.
        calls.append(
            (path, args[:1] if path == mirror and args[0] == "fetch" else args)
        )
        if args[0] == "clone":
            (workspace / ".git").mkdir()
        return responses.get(args[0], "")

    monkeypatch.setattr(worktree_git, "git", checked)

    assert worktree_checkout.create(clone, "protocol", request) == workspace
    reference = "refs/heads/main" if profile == "build" else full_sha
    checkout = (
        ("checkout", "-b", "agent/protocol", full_sha, "--")
        if profile == "build"
        else ("checkout", "--detach", full_sha, "--")
    )
    options = ("clone", "--no-local", "--no-hardlinks", "--no-checkout", "--template=")
    helper = ("config", "--add", "credential.https://github.com.helper")
    values = ("", gh_refresh.credential_helper()) if profile == "build" else ()
    assert calls == [
        (mirror, ("init", "--bare", "--template=")),
        (mirror, (*options, str(mirror), str(workspace))),
        (
            workspace,
            (
                "config",
                "remote.origin.pushurl",
                "https://github.com/tbhb-dev/example-repo.git",
            ),
        ),
        (workspace, ("config", "core.hooksPath", str(workspace / ".git" / "hooks"))),
        *((workspace, (*helper, value)) for value in values),
        (mirror, ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD")),
        (mirror, ("check-ref-format", "--branch", "main")),
        (mirror, ("fetch",)),
        (
            mirror,
            ("rev-parse", "--verify", "--end-of-options", reference + "^{commit}"),
        ),
        (
            workspace,
            (
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                str(mirror),
                "+refs/heads/*:refs/remotes/origin/*",
            ),
        ),
        (workspace, checkout),
    ]
    assert workspace.is_dir()
    assert workspace.stat().st_mode & 0o777 == 0o700
    assert (workspace / ".git" / "hooks" / "pre-commit").is_file()
    assert (workspace / ".git/config.worktree").read_text() == ""
    assert (workspace / ".git/commondir").read_text() == ".\n"
    assert list((workspace / ".git/modules").iterdir()) == []
    assert (mirror / "config").read_text() == worktree_mirror.MIRROR_CONFIG.format(
        url="https://github.com/tbhb-dev/example-repo.git"
    )
    assert {p.name for p in mirror.iterdir()} == {"config", "agent-mirror.lock"}


def test_run_clone_parent_cannot_redirect_git_into_a_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    (clone / ".worktrees").symlink_to(cache, target_is_directory=True)

    def unexpected(*_args: object) -> str:
        pytest.fail("host git must not enter a run container in a shared cache")

    monkeypatch.setattr(worktree_git, "git", unexpected)
    with pytest.raises(
        Invalid, match=r"^run clone parent must not be a symbolic link$"
    ):
        worktree_checkout.create(clone, "example", run_request())
    assert list(cache.iterdir()) == []


class Stop(Exception):  # noqa: N818 -- a test sentinel, not an error type
    pass


def test_private_fetch_without_a_token_source_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    urls: list[str] = []
    original = worktree_mirror.configure_mirror

    def configure(mirror: Path, url: str) -> None:
        urls.append(url)
        original(mirror, url)

    def fetch(_mirror: Path, _request: object) -> str:
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(worktree_mirror, "configure_mirror", configure)
    monkeypatch.setattr(worktree_mirror, "fetch_commit", fetch)
    with pytest.raises(Invalid, match="mirror fetch"):
        worktree_checkout.create(clone, "p1", run_request())
    assert urls == ["https://github.com/tbhb-dev/example-repo.git"]


def test_private_fetch_retries_over_https_with_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    urls: list[str] = []
    tokens: list[str | None] = []
    original = worktree_mirror.configure_mirror
    request = run_request()

    def configure(mirror: Path, url: str) -> None:
        urls.append(url)
        original(mirror, url)

    def fetch(mirror: Path, actual: object, token: str | None = None) -> str:
        assert mirror == worktree_mirror.mirror_path(tmp_path, request.repo)
        assert actual is request
        tokens.append(token)
        if token is None:
            raise subprocess.CalledProcessError(128, "git")
        raise Stop

    monkeypatch.setattr(worktree_mirror, "configure_mirror", configure)
    monkeypatch.setattr(worktree_mirror, "fetch_commit", fetch)
    with pytest.raises(Stop):
        worktree_checkout.create(
            clone,
            "p3",
            request,
            lambda: "example-token",
        )
    assert urls == ["https://github.com/tbhb-dev/example-repo.git"]
    assert tokens == [None, "example-token"]


def test_private_build_fetch_retries_over_https_with_the_pull_requests_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = {
        "example/pr/client": "stub-pr-client",
        "example/pr/pem": "stub-pr-pem",
        "example/gh/value": "stub-gh",
    }
    reads: list[str] = []
    minted: list[dict[str, object]] = []

    def read(reference: str) -> str:
        reads.append(reference)
        return store[reference]

    def mint(_env: object, **keywords: object) -> gh_app_api.Minted:
        minted.append(keywords)
        return gh_app_api.Minted("stub-fetch-token", 2000)

    monkeypatch.setattr(secrets, "read", read)
    monkeypatch.setattr(gh_app_api, "mint_token", mint)
    _, clone = repository(tmp_path)
    request = run_request()
    settings = pull_requests_app_settings()
    tokens: list[str | None] = []

    def fetch(_mirror: Path, _request: object, token: str | None = None) -> str:
        tokens.append(token)
        if token is None:
            raise subprocess.CalledProcessError(128, "git")
        raise Stop

    monkeypatch.setattr(worktree_mirror, "fetch_commit", fetch)
    with pytest.raises(Stop):
        worktree_checkout.create(
            clone,
            "p5",
            request,
            lambda: credentials.fetch_token(request, settings),
        )
    assert tokens == [None, "stub-fetch-token"]
    assert reads == ["example/pr/client", "example/pr/pem"]
    assert [m["permissions"] for m in minted] == [{"contents": "read"}]
    assert [m["repository"] for m in minted] == [request.repo]


def test_private_fetch_with_no_token_fails_without_an_ssh_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    urls: list[str] = []
    original = worktree_mirror.configure_mirror

    def configure(mirror: Path, url: str) -> None:
        urls.append(url)
        original(mirror, url)

    def fetch(_mirror: Path, _request: object, token: str | None = None) -> str:
        assert token is None
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(worktree_mirror, "configure_mirror", configure)
    monkeypatch.setattr(worktree_mirror, "fetch_commit", fetch)
    with pytest.raises(Invalid, match="mirror fetch"):
        worktree_checkout.create(clone, "p4", run_request(), lambda: None)
    assert urls == ["https://github.com/tbhb-dev/example-repo.git"]


def test_outside_repository_has_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def unexpected(*_args: object) -> str:
        pytest.fail("unlisted repository must never invoke git")

    monkeypatch.setattr(worktree_git, "git", unexpected)
    before = list(tmp_path.rglob("*"))
    with pytest.raises(Invalid) as caught:
        worktree_checkout.create(
            tmp_path, "example", run_request(repo="example-org/example-repo")
        )
    assert str(caught.value) == (
        "repository example-org/example-repo: agent mirrors fetch only tbhb-dev "
        "repositories"
    )
    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("name", "reaches_fetch"),
    [
        ("tbhb-dev/new-repo", True),
        ("github.com/TBHB-Dev/new-repo", True),
        ("git.example.com/tbhb-dev/new-repo", False),
        ("tbhb-dev-other/new-repo", False),
    ],
)
def test_org_repositories_need_no_list_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    reaches_fetch: bool,
) -> None:
    def reached(_clone: Path) -> None:
        message = "reached the clone directory"
        raise Invalid(message)

    monkeypatch.setattr(worktree_checkout, "ensure_clone_directory", reached)
    match = "reached the clone" if reaches_fetch else "only tbhb-dev repositories"
    with pytest.raises(Invalid, match=match):
        worktree_checkout.create(tmp_path, "example", run_request(repo=name))


@pytest.mark.parametrize(
    ("operation", "step"),
    [
        ((runfiles, "private_dir"), "mirror directory"),
        ((worktree_mirror, "configure_mirror"), "mirror init"),
        ((worktree_mirror, "fetch_commit"), f"mirror fetch ({URL})"),
        ((worktree_clone, "clone_run"), "clone"),
        ((worktree_hooks, "install_hooks"), "hook install"),
        ((worktree_git, "git"), "checkout"),
        ((worktree_clone, "prepare_git"), "git metadata"),
    ],
)
@pytest.mark.parametrize(
    "failure",
    [
        (OSError, "[redacted]"),
        (ValueError, "invalid value"),
        (Invalid, "[redacted]"),
        (subprocess.CalledProcessError, "[redacted]"),
    ],
)
def test_worktree_failure_names_its_step_and_safe_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: tuple[ModuleType, str],
    step: str,
    failure: tuple[type[Exception], str],
) -> None:
    module, target = operation
    error, cause = failure
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("EXAMPLE_CREDENTIAL", "sensitive-exception")
    monkeypatch.setenv("EXAMPLE_STDERR", "sensitive-stderr")
    monkeypatch.setattr(worktree_git, "git", constant(""))
    monkeypatch.setattr(worktree_mirror, "configure_mirror", constant(None))
    monkeypatch.setattr(worktree_mirror, "fetch_commit", constant("a" * 40))
    monkeypatch.setattr(worktree_clone, "clone_run", constant(None))
    monkeypatch.setattr(worktree_hooks, "install_hooks", constant(None))
    monkeypatch.setattr(worktree_clone, "prepare_git", constant(None))

    fail = partial(preparation_failure, error)

    monkeypatch.setattr(module, target, fail)
    with pytest.raises(Invalid) as caught:
        worktree_checkout.create(tmp_path, "example", run_request())
    assert str(caught.value) == f"run preparation failed at {step}: {cause}"
    assert "sensitive" not in str(caught.value)


@pytest.fixture
def run_tools(
    scratch_cwd: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del scratch_cwd
    binary = tmp_path / "bin"
    binary.mkdir()
    write_fake(
        binary / "fake-agent-cli",
        "import sys\n"
        "from tbhbdev.alpha.pre_push_guard import main\n"
        "if sys.argv[1:3] == ['alpha', 'identity']:\n"
        "    sys.exit(0)\n"
        "assert sys.argv[1:3] == ['alpha', 'pre-push-guard']\n"
        "sys.exit(main(sys.argv[3:], 'pre-push-guard'))\n",
    )
    (binary / "tbhbdev").symlink_to("fake-agent-cli")
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(
        "PYTHONPATH", pythonpath(Path(pre_push_guard.__file__).parents[2])
    )
    # Keep the installed hook's interpreter path within the shebang limit.
    prefix = tmp_path / "python"
    prefix.symlink_to(Path(sys.base_prefix), target_is_directory=True)
    monkeypatch.setattr(sys, "base_prefix", str(prefix))


@pytest.mark.usefixtures("run_tools")
@pytest.mark.parametrize(
    ("remote_state", "error"),
    [
        ("current", ""),
        ("advanced", "stale info"),
        ("missing-fetch", "the remote tip is not the fetched one"),
    ],
)
def test_start_ref_rebase_and_push(
    tmp_path: Path, remote_state: str, error: str
) -> None:
    seed, clone = repository(tmp_path)
    remote = tmp_path / "origin"
    branch = "refs/heads/fix/topic"
    tracking = "refs/remotes/origin/fetched/heads/fix/topic"
    git(seed, "switch", "-c", "fix/topic")
    (seed / "topic").write_text("topic change\n")
    start = commit(seed, "topic")
    git(seed, "push", "origin", branch)
    git(seed, "switch", "main")
    upstream = commit(seed, "upstream")
    git(seed, "push", "origin", "main")

    request = run_request(start_ref=branch, fetch_refs=[branch])
    workspace = worktree_checkout.create(clone, "example", request)
    assert git(workspace, "for-each-ref", "refs/remotes/origin/fix/topic") == ""
    expected = git(workspace, "rev-parse", "--verify", tracking)
    assert expected == start
    git(workspace, "config", "remote.origin.pushurl", str(remote))

    files = runfiles.RunFiles(tmp_path / "state", "example")
    stop = run_stop_hook.run_context(request, str(workspace), files, tmp_path, start)
    assert run_stop_hook.build_reason(stop) is not None
    before, _ = push.local_refs(workspace)
    git(
        workspace,
        "-c", "user.name=Example",
        "-c", "user.email=example@example.test",
        "-c", "commit.gpgsign=false",
        "rebase", "origin/main",
    )  # fmt: skip
    assert git(workspace, "rev-parse", "HEAD^") == upstream
    rewritten = git(workspace, "rev-parse", "HEAD")
    after, checkout = push.local_refs(workspace)
    assert push.new_commits(
        Mock(request=request, files=files), workspace, before, after, checkout
    ) == [("refs/heads/agent/example", rewritten)]

    remote_tip = start
    if remote_state == "advanced":
        git(seed, "switch", "fix/topic")
        remote_tip = commit(seed, "concurrent topic change")
        git(seed, "push", "origin", branch)
    elif remote_state == "missing-fetch":
        git(workspace, "update-ref", "-d", tracking)

    def publish(tip: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "push",
                f"--force-with-lease={branch}:{tip}",
                "origin",
                f"HEAD:{branch}",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    result = publish(expected)
    assert bool(result.returncode) == bool(error), result.stderr
    assert (error or f"{branch} rewritten {start} -> {rewritten}") in result.stderr
    if not error:
        assert run_stop_hook.build_reason(stop) is None
        remote_tip = rewritten
    elif remote_state == "advanced":
        result = publish(remote_tip)
        assert result.returncode != 0
        assert "remote tip changed since fetch" in result.stderr
    assert git(remote, "rev-parse", branch) == remote_tip


@pytest.mark.usefixtures("run_tools")
def test_start_ref_accepts_the_tip_this_run_pushed(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    remote = tmp_path / "origin"
    branch = "refs/heads/fix/topic"
    git(seed, "switch", "-c", "fix/topic")
    commit(seed, "topic")
    git(seed, "push", "origin", branch)
    request = run_request(start_ref=branch, fetch_refs=[branch])
    workspace = worktree_checkout.create(clone, "example", request)
    git(workspace, "config", "remote.origin.pushurl", str(remote))

    def publish() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "push", "origin", f"HEAD:{branch}"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    first = commit(workspace, "first")
    assert publish().returncode == 0
    assert git(remote, "rev-parse", branch) == first
    second = commit(workspace, "second")
    result = publish()
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", branch) == second

    git(seed, "fetch", "origin")
    git(seed, "reset", "--hard", "origin/fix/topic")
    other = commit(seed, "other writer")
    git(seed, "push", "origin", branch)
    commit(workspace, "third")
    result = publish()
    assert result.returncode != 0
    assert f"{branch}: remote tip changed since fetch; push refused" in result.stderr
    assert git(remote, "rev-parse", branch) == other
