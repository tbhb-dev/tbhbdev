"""Mirror configuration and fetching requested refs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Unpack

import pytest
from agent_host_example import (
    HostGitOptions,
    constant,
    git,
    repository,
    run_request,
)

from tbhbdev.alpha.agent import (
    worktree_checkout,
    worktree_clone,
    worktree_git,
    worktree_hooks,
    worktree_mirror,
)
from tbhbdev.alpha.agent.model import Invalid

pytestmark = pytest.mark.usefixtures("scratch_cwd")

URL = "https://github.com/tbhb-dev/example-repo.git"


@pytest.mark.parametrize("token", [None, "example-token"])
def test_named_refs_share_the_default_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fetch(_path: Path, *args: str, token: str | None = None) -> str:
        calls.append((args, token))
        if args[0] == "ls-remote":
            return "ref: refs/heads/main\tHEAD"
        return "a" * 40

    monkeypatch.setattr(worktree_git, "git", fetch)
    request = run_request(fetch_refs=["refs/pull/391/head", "refs/heads/fix-392"])
    assert worktree_mirror.fetch_commit(tmp_path, request, token) == "a" * 40
    assert [call for call in calls if call[0][0] == "fetch"] == [
        (
            (
                "fetch",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--no-tags",
                "--upload-pack=git-upload-pack",
                "--no-prune",
                "origin",
                "+refs/heads/main:refs/heads/main",
                "+refs/pull/391/head:refs/heads/fetched/pull/391/head",
                "+refs/heads/fix-392:refs/heads/fetched/heads/fix-392",
            ),
            token,
        )
    ]


@pytest.mark.parametrize("state", [None, "", "/example/state"])
def test_mirror_location_uses_the_host_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    if state is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", state)
    base = Path(state) if state else tmp_path / ".local" / "state"
    assert worktree_mirror.mirror_path(tmp_path, "example-org/example-repo") == (
        base
        / "tbhbdev"
        / "agent-mirrors"
        / "github.com"
        / "example-org"
        / "example-repo.git"
    )


@pytest.mark.parametrize(
    "listing",
    ["", "abc\tHEAD", "ref: refs/tags/main\tHEAD", "ref: refs/heads/main HEAD"],
)
def test_default_branch_requires_a_branch_symref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    def listing_only(path: Path, *args: str) -> str:
        assert path == tmp_path
        assert args == ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD")
        return listing

    monkeypatch.setattr(worktree_git, "git", listing_only)
    with pytest.raises(Invalid, match=r"^origin HEAD does not name a branch$"):
        worktree_mirror.default_branch(tmp_path)


@pytest.mark.parametrize("commit_id", ["a" * 40, "b" * 64, "invalid"])
def test_fetch_resolves_only_a_full_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_id: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(path: Path, *args: str) -> str:
        assert path == tmp_path
        calls.append(args)
        return "ref: refs/heads/main\tHEAD" if args[0] == "ls-remote" else commit_id

    monkeypatch.setattr(worktree_git, "git", run)
    request = run_request(profile="review", commit="0" * 40)
    if commit_id == "invalid":
        with pytest.raises(Invalid, match=r"^mirror did not resolve a full commit$"):
            worktree_mirror.fetch_commit(tmp_path, request)
    else:
        assert worktree_mirror.fetch_commit(tmp_path, request) == commit_id
    options = ("--no-recurse-submodules", "--no-write-fetch-head", "--no-tags")
    assert calls == [
        ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD"),
        ("check-ref-format", "--branch", "main"),
        (
            "fetch",
            *options,
            "--upload-pack=git-upload-pack",
            "--no-prune",
            "origin",
            "+refs/heads/main:refs/heads/main",
            f"+{'0' * 40}:refs/heads/tbhbdev-review",
        ),
        ("rev-parse", "--verify", "--end-of-options", f"{'0' * 40}^{{commit}}"),
    ]


def test_mirror_configuration_and_hooks_are_host_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    global_config = tmp_path / "global-config"
    global_config.write_text(f"[core]\nhooksPath = {tmp_path / 'other-hooks'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    path = worktree_checkout.create(clone, "example", run_request())
    mirror = worktree_mirror.mirror_path(tmp_path, run_request().repo)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        git(mirror, "config", "--get-all", "remote.origin.fetch")
    assert caught.value.returncode == 1
    for key, value in (
        ("gc.auto", "0"),
        ("gc.pruneExpire", "never"),
        ("core.logAllRefUpdates", "false"),
        ("maintenance.auto", "false"),
        ("fetch.recurseSubmodules", "false"),
        ("submodule.recurse", "false"),
    ):
        assert git(mirror, "config", key) == value
    assert not (mirror / "FETCH_HEAD").exists()
    assert git(path, "config", "--get", "core.hooksPath") == str(
        path / ".git" / "hooks"
    )
    for name in (
        "pre-commit",
        "pre-push.prek",
        "prepare-commit-msg",
        "commit-msg",
        "post-commit",
    ):
        hook = path / ".git" / "hooks" / name
        assert hook.stat().st_mode & 0o777 == 0o555
        assert hook.read_text().startswith("#!/bin/sh\n")


def test_mirror_fetch_never_recurses_into_a_configured_module(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    first = worktree_checkout.create(clone, "first", run_request())
    mirror = worktree_mirror.mirror_path(tmp_path, run_request().repo)
    head = git(first, "rev-parse", "HEAD")
    git(mirror, "update-index", "--add", "--cacheinfo", f"160000,{head},x")
    module = mirror / "modules" / "x"
    module.parent.mkdir()
    git(mirror, "init", str(module))
    marker = tmp_path / "module-marker"
    script = tmp_path / "upload"
    script.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\n')
    script.chmod(0o700)
    git(module, "config", "remote.origin.url", str(tmp_path / "origin"))
    git(module, "config", "remote.origin.uploadpack", str(script))
    git(mirror, "config", "fetch.recurseSubmodules", "true")
    git(mirror, "config", "submodule.x.active", "true")
    worktree_checkout.create(clone, "second", run_request())
    assert not marker.exists()


def test_default_branch_validates_the_short_branch_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def checked(path: Path, *args: str) -> str:
        assert path == tmp_path
        calls.append(args)
        return "ref: refs/heads/topic/main\tHEAD\nabc\tHEAD"

    monkeypatch.setattr(worktree_git, "git", checked)
    assert worktree_mirror.default_branch(tmp_path) == "refs/heads/topic/main"
    assert calls == [
        ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD"),
        ("check-ref-format", "--branch", "topic/main"),
    ]


def test_existing_mirror_config_is_replaced_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config").write_text(
        worktree_mirror.MIRROR_CONFIG.format(
            url="git@github.com:tbhb-dev/example-repo.git"
        )
    )
    probes: list[Path] = []
    original = Path.exists

    def exists(path: Path) -> bool:
        probes.append(path)
        return original(path)

    def unexpected(*_args: object) -> str:
        pytest.fail("existing mirror config must be replaced before invoking git")

    monkeypatch.setattr(worktree_git, "git", unexpected)
    monkeypatch.setattr(Path, "exists", exists)
    worktree_mirror.configure_mirror(
        tmp_path, "https://github.com/tbhb-dev/example-repo.git"
    )
    assert probes == [tmp_path / "config"]
    assert (tmp_path / "config").read_text() == worktree_mirror.MIRROR_CONFIG.format(
        url="https://github.com/tbhb-dev/example-repo.git"
    )


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_mirror_owned_paths_never_follow_aliases_before_host_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    request = run_request()
    mirror = worktree_mirror.mirror_path(tmp_path, request.repo)
    path = mirror if depth == 0 else mirror.parents[depth - 1]
    path.parent.mkdir(parents=True)
    target = tmp_path / "untouched"
    target.mkdir()
    target.chmod(0o750)
    (target / "marker").write_text("original")
    path.symlink_to(target, target_is_directory=True)

    def unexpected(*_args: object) -> str:
        pytest.fail("host git must never enter an alias for a mirror directory")

    monkeypatch.setattr(worktree_git, "git", unexpected)
    with pytest.raises(
        Invalid, match=r"^host mirror paths must not be symbolic links$"
    ):
        worktree_checkout.create(clone, "example", request)
    assert {p.name for p in target.iterdir()} == {"marker"}
    assert (target / "marker").read_text() == "original"
    assert target.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize(
    ("host", "component"),
    [
        ("github.com", "github.com"),
        ("GitHub.com", "github.com"),
        ("git.example.com", "git.example.com"),
    ],
)
def test_mirror_path_matches_driver_contract(
    tmp_path: Path, host: str, component: str
) -> None:
    assert worktree_mirror.mirror_path(
        tmp_path, f"{host}/example-org/example-repo"
    ) == (
        tmp_path
        / "state"
        / "tbhbdev"
        / "agent-mirrors"
        / component
        / "example-org"
        / "example-repo.git"
    )


def test_fetch_scope_never_fetches_or_prunes_agent_namespace(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    head = git(seed, "rev-parse", "HEAD")
    for ref in (
        "refs/heads/a",
        "refs/heads/agent/a",
        "refs/pull/1/head",
        "refs/tags/v1",
    ):
        git(seed, "push", "origin", f"{head}:{ref}")
    worktree_checkout.create(clone, "first", run_request())
    mirror = worktree_mirror.mirror_path(tmp_path, run_request().repo)
    assert git(mirror, "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/main",
    ]
    git(mirror, "update-ref", "refs/heads/agent/a", head)
    git(mirror, "update-ref", "refs/tbhbdev/default", head)
    git(mirror, "update-ref", "refs/heads/kept", head)
    for ref in ("refs/heads/a", "refs/heads/agent/a"):
        git(seed, "push", "origin", f":{ref}")
        git(seed, "push", "origin", f"{head}:{ref}/b")
    worktree_checkout.create(clone, "second", run_request())
    assert git(mirror, "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/agent/a",
        "refs/heads/kept",
        "refs/heads/main",
        "refs/tbhbdev/default",
    ]


def test_new_mirror_never_discovers_parent_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    git(clone, "config", "include.path", str(tmp_path / "broken-config"))
    (tmp_path / "broken-config").write_text("[broken\n")
    mirror = clone / "nested.git"
    mirror.mkdir()
    calls: list[list[str]] = []
    output = subprocess.check_output

    def checked(command: list[str], **kwargs: Unpack[HostGitOptions]) -> str:
        calls.append(command)
        return output(command, **kwargs)

    monkeypatch.chdir(clone)
    monkeypatch.setattr(subprocess, "check_output", checked)
    worktree_mirror.configure_mirror(mirror, str(tmp_path / "origin"))
    assert len(calls) == 1
    assert "--git-dir" in calls[0]
    assert "-C" not in calls[0]
    assert "--get-regexp" not in calls[0]
    assert (mirror / "HEAD").is_file()


@pytest.mark.parametrize(
    "reference", ["refs/heads/a..b", "refs/heads/a@{b", "refs/heads/a.lock"]
)
def test_remote_branch_names_pass_real_git_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    repository(tmp_path)
    mirror = tmp_path / "origin"
    original = worktree_git.git

    def checked(path: Path, *args: str) -> str:
        if args[0] == "ls-remote":
            return f"ref: {reference}\tHEAD"
        return original(path, *args)

    monkeypatch.setattr(worktree_git, "git", checked)
    with pytest.raises(subprocess.CalledProcessError):
        worktree_mirror.default_branch(mirror)


def test_fetch_commit_passes_the_token_to_the_fetch_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []

    def git(_path: Path, *args: str, token: str | None = None) -> str:
        calls.append((args[0], token))
        if args[0] == "ls-remote":
            return "ref: refs/heads/main\tHEAD"
        return "a" * 40

    monkeypatch.setattr(worktree_git, "git", git)
    value = "example-credential"
    worktree_mirror.fetch_commit(tmp_path, run_request(), value)
    assert calls == [
        ("ls-remote", value),
        ("check-ref-format", None),
        ("fetch", value),
        ("rev-parse", None),
    ]


@pytest.mark.parametrize("existing", [False, True])
def test_public_mirror_uses_https_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, existing: bool
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    request = run_request()
    mirror = worktree_mirror.mirror_path(tmp_path, request.repo)
    if existing:
        mirror.mkdir(parents=True)
        (mirror / "config").write_text(
            '[remote "origin"]\nurl = git@github.com:tbhb-dev/example-repo.git\n'
        )
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def git(path: Path, *args: str) -> str:
        calls.append((path, args))
        if args[0] != "init":
            assert (
                mirror / "config"
            ).read_text() == worktree_mirror.MIRROR_CONFIG.format(url=URL)
        if args[0] == "ls-remote":
            return "ref: refs/heads/main\tHEAD"
        return "a" * 40 if args[0] == "rev-parse" else ""

    monkeypatch.setattr(worktree_git, "git", git)
    monkeypatch.setattr(worktree_hooks, "install_hooks", constant(None))
    monkeypatch.setattr(worktree_clone, "prepare_git", constant(None))
    workspace = worktree_checkout.create(tmp_path, "example", request)
    assert workspace == tmp_path / ".worktrees" / "agent-example"
    assert (
        (mirror, ("init", "--bare", "--template=")) in calls
        if not existing
        else (all(args[0] != "init" for _path, args in calls))
    )
    assert all(args[:2] != ("config", "remote.origin.url") for _p, args in calls)
    assert (workspace, ("config", "remote.origin.pushurl", URL)) in calls
    assert any(args[0] == "fetch" for _path, args in calls)
