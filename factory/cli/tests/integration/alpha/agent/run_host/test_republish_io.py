"""Exercise moved-start-ref republication with real Git, ssh-agent, and Gitleaks."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_host_example import commit, git, make_run, repository
from fake_tool import PRIVATE_CONFIG, RULES_BEYOND_ONE
from run_host_fixtures import HOST_FIXTURES, preparation

from tbhbdev.alpha import pre_push_guard as guard
from tbhbdev.alpha.agent import credentials, host_tools, process_group, push, rescue
from tbhbdev.alpha.agent.run_host import execution, harness, preflight, publication

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host import state

__all__ = ["pytestmark"]
pytestmark = HOST_FIXTURES

BRANCH, START_REF = "agent/example", "refs/heads/topic"
MISSING = "push failed: build commit missing from topic on origin\n"
CONFLICT_LOG = f"republish: conflict, published to {BRANCH}\n"
FOUND_LOG = "republish: kept local: secret scan found a secret\n"
RESCUE_LOG = "fault publication: kept local: start ref moved\n"


def agent_commit(repo: Path, key: Path | None, msg: str) -> str:
    git(repo, "add", ".")
    who = {"NAME": "tbhb.dev Agent", "EMAIL": guard.AGENT_EMAIL}
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    for r in ("AUTHOR", "COMMITTER"):
        env |= {f"GIT_{r}_{k}": v for k, v in who.items()}
    env.pop("SSH_AUTH_SOCK", None)
    cfg = ["core.hooksPath=/dev/null"]
    cfg += ["gpg.format=ssh", f"user.signingkey={key}"] if key else []
    opts = [x for item in cfg for x in ("-c", item)]
    git(repo, *opts, "commit", "-S" if key else "--no-gpg-sign", "-qm", msg, env=env)
    return git(repo, "rev-parse", "HEAD")


def build_repo(tmp_path: Path, case: str) -> tuple[Path, Path, Path, str, str, str]:
    seed, ws = repository(tmp_path / "repo")
    origin = tmp_path / "repo" / "origin"
    (seed / ".gitleaks.toml").touch()
    base = commit(seed, "scan")
    ref = "refs/heads/main" if case == "protected" else START_REF
    git(seed, "push", "origin", "main")
    if case not in ("missing", "protected"):
        git(seed, "push", "origin", f"main:{ref}")
    git(ws, "fetch", "origin", "main")
    git(ws, "switch", "-c", BRANCH, base)
    moved = base
    if case not in ("unchanged", "missing"):
        (seed / ("tracked" if case == "conflict" else "other.txt")).write_text("r\n")
        moved = commit(seed, "remote move")
        git(seed, "push", "origin", f"HEAD:{ref}")
    key = tmp_path / "k"
    cmd = ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)]
    subprocess.run(cmd, check=True, capture_output=True)
    pub = Path(os.environ["HOME"]) / guard.AGENT_KEY
    pub.parent.mkdir(parents=True, exist_ok=True)
    pub.write_text(key.with_suffix(".pub").read_text())
    if case == "rewritten":
        git(ws, "reset", "--hard", "HEAD^")
    name = "tracked" if case == "conflict" else "run.txt"
    text = (
        "run\n" if case == "conflict" else ("host-x\n" if case == "secret" else "ok\n")
    )
    (ws / name).write_text(text)
    head = agent_commit(ws, None if case == "unsigned_orig" else key, "agent fix")
    return origin, seed, ws, base, head, moved


@pytest.fixture
def scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> tuple[state.Run, Path, str, str, Mock]:
    private = tmp_path / "private.toml"
    private.write_text(PRIVATE_CONFIG + RULES_BEYOND_ONE)
    origin, seed, ws, base, head, moved = build_repo(tmp_path, case)
    no_sign = case in ("unsigned", "unsigned_orig")
    key_text = None if no_sign else (tmp_path / "k").read_text()
    ref = "refs/heads/main" if case == "protected" else START_REF
    run = make_run(tmp_path / "r", expect_commit=True, start_ref=ref, fetch_refs=[ref])
    prep = preparation(run)
    sock = str(run.tmpdir / "ssh.sock") if key_text else None
    paths = dataclasses.replace(prep.paths, worktree=str(ws), signing_socket=sock)
    delivery = credentials.Delivery({}, key_text, claude_profile=None)
    ready = dataclasses.replace(prep, paths=paths, delivery=delivery)

    def fake_push(*_args: object) -> int:
        if case in ("conflict", "unsigned", "second_refusal"):
            run.files.write("launched", "\n")
        run.files.write("base", base)
        run.files.write("head", head)
        return 0

    orig, twice = push.remote_git, False

    def local_remote(path: Path, *args: str, token: str | None = None) -> str:
        nonlocal twice
        assert path != ws
        if case == "second_refusal" and not twice and args and args[0] == "push":
            twice = True
            (seed / "third.txt").write_text("second\n")
            commit(seed, "second move")
            git(seed, "push", "origin", f"HEAD:{ref}")
        rewritten = tuple(str(origin) if a.startswith("https://") else a for a in args)
        return orig(path, *rewritten, token=token)

    remote = Mock(wraps=local_remote)
    patches = (
        (rescue.secret_scan, "private_config", lambda: private),
        (guard, "public_keys", Mock(side_effect=OSError)),
        (host_tools, "search", lambda: os.environ["PATH"]),
        (credentials, "github_token", Mock(return_value=None)),
        (preflight, "preflight", Mock()),
        (process_group, "prepare", Mock(return_value=ready)),
        (harness, "run_harness", Mock(return_value=0)),
        (publication, "origin_contains", Mock(return_value=False)),
        (push, "run", fake_push),
        (push, "remote_git", remote),
    )
    for target, attr, value in patches:
        monkeypatch.setattr(target, attr, value)
    return run, origin, head, moved, remote


@pytest.mark.parametrize(
    ("case", "rc", "branch", "snippet"),
    [
        ("replay", 0, "topic", "republish: replayed onto "),
        ("conflict", 12, BRANCH, CONFLICT_LOG),
        ("unsigned", 12, BRANCH, CONFLICT_LOG),
        ("rewritten", 12, BRANCH, CONFLICT_LOG),
        ("unchanged", 9, None, MISSING),
        ("secret", 9, None, FOUND_LOG),
        ("second_refusal", 9, None, RESCUE_LOG),
        ("missing", 9, None, "republish: kept local: start ref missing\n"),
        ("protected", 9, None, "republish: kept local: refs/heads/main:"),
        (
            "unsigned_orig",
            9,
            None,
            "republish: kept local: agent commit signature refused\n",
        ),
    ],
)
def test_republish(
    scene: tuple[state.Run, Path, str, str, Mock],
    case: str,
    rc: int,
    branch: str | None,
    snippet: str,
) -> None:
    run, origin, head, moved, remote = scene
    assert execution.execute(run, "example") == rc
    assert run.files.path("rc").read_text() == f"{rc}\n"
    log = run.files.path("log").read_text()
    assert snippet in log
    bp = run.files.path("branch")
    assert (bp.read_text() if bp.exists() else None) == branch
    if case == "replay":
        tip = git(origin, "rev-parse", START_REF)
        assert f"republish: replayed onto {moved[:12]} and pushed\n" in log
        assert tip != moved
        assert git(origin, "merge-base", moved, tip) == moved
        assert git(origin, "show", f"{tip}:run.txt") == "ok"
        assert git(origin, "show", f"{tip}:other.txt") == "r"
        keys = guard.SigningKeys()
        assert not guard.agent_commit_problems(START_REF, tip, [moved], keys, origin)
    elif case in ("conflict", "unsigned", "rewritten"):
        assert "fault publication:" not in log
        assert git(origin, "rev-parse", START_REF) == moved
        assert git(origin, "rev-parse", f"refs/heads/{BRANCH}") == head
    elif case == "unchanged":
        assert "republish:" not in log
        assert {e.args[1] for e in remote.call_args_list}.isdisjoint({"fetch", "push"})
    else:
        assert not git(origin, "for-each-ref", f"refs/heads/{BRANCH}")
        if case == "secret":
            assert git(origin, "rev-parse", START_REF) == moved
        elif case == "second_refusal":
            assert "republish: kept local:" in log
            assert run.files.path("head").read_text().strip() == head
            assert sum(e.args[1] == "push" for e in remote.call_args_list) == 1
